"""Optional WSL-local virtual UPnP IGD for unchanged sandbox applications.

SSDP M-SEARCH is answered inside WSL. SOAP calls go over the separate
HMAC-authenticated bridge to Windows, where the actual IGD mapping and host
TCP/UDP forwarding are owned. This is NOT a raw UPnP proxy to the LAN.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import os
import socket
import struct
import sys
import threading
import uuid
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from syn_wsl_bridge_common import BridgeClient, BridgeError, secret_file, valid_ipv4, valid_port

LOG = logging.getLogger("syn.wsl.virtual-igd")
SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
IGD = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
WAN_IP = "urn:schemas-upnp-org:service:WANIPConnection:1"
WAN_DEVICE = "urn:schemas-upnp-org:device:WANDevice:1"
WAN_CONN = "urn:schemas-upnp-org:device:WANConnectionDevice:1"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
MAX_SOAP_BYTES = 8192
SSDP_TARGETS = {"ssdp:all", "upnp:rootdevice", IGD, WAN_IP, WAN_DEVICE, WAN_CONN}
ROOT_PATH = "/upnp/root.xml"
CONTROL_PATH = "/upnp/control/WANIPConn1"
SERVICE_PATH = "/upnp/service/WANIPConn1.xml"


class SoapFault(ValueError):
    def __init__(self, code: int, description: str):
        super().__init__(description)
        self.code, self.description = code, description


def parse_msearch(raw: bytes) -> str | None:
    """Only respond to bounded, valid M-SEARCH discovery requests."""
    if not raw or len(raw) > 2048:
        return None
    try: text = raw.decode("ascii")
    except UnicodeError: return None
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0].strip().upper() != "M-SEARCH * HTTP/1.1":
        return None
    fields = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip().strip('"').lower()
    search = fields.get("st", "")
    if (fields.get("man") != "ssdp:discover" or
            search not in {x.lower() for x in SSDP_TARGETS}):
        return None
    return search


def ssdp_reply(search: str, address: str, port: int, udn: str) -> bytes:
    target = IGD if search == "ssdp:all" else next(
        s for s in SSDP_TARGETS if s.lower() == search)
    return ("HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=120\r\n"
            "EXT:\r\n"
            f"LOCATION: http://{address}:{port}{ROOT_PATH}\r\n"
            "SERVER: Linux/WSL UPnP/1.0 Syn-WSL-Bridge/1\r\n"
            f"ST: {target}\r\n"
            f"USN: uuid:{udn}::{target}\r\n\r\n").encode("ascii")


def root_description(address: str, port: int, udn: str) -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?>'
      f'<root xmlns="urn:schemas-upnp-org:device-1-0"><specVersion><major>1</major><minor>0</minor></specVersion>'
      f'<URLBase>http://{address}:{port}/</URLBase><device>'
      f'<deviceType>{IGD}</deviceType><friendlyName>Syn WSL Virtual Gateway</friendlyName>'
      f'<manufacturer>Syn</manufacturer><modelName>Host-owned WSL2 NAT bridge</modelName>'
      f'<UDN>uuid:{udn}</UDN><deviceList><device><deviceType>{WAN_DEVICE}</deviceType>'
      f'<friendlyName>Windows LAN WAN</friendlyName><manufacturer>Syn</manufacturer>'
      f'<modelName>Windows WSL bridge</modelName><UDN>uuid:{udn}</UDN><deviceList><device>'
      f'<deviceType>{WAN_CONN}</deviceType><friendlyName>Bridged WAN</friendlyName>'
      f'<manufacturer>Syn</manufacturer><modelName>Bridge WAN connection</modelName>'
      f'<UDN>uuid:{udn}</UDN><serviceList><service><serviceType>{WAN_IP}</serviceType>'
      f'<serviceId>urn:upnp-org:serviceId:WANIPConn1</serviceId>'
      f'<SCPDURL>{SERVICE_PATH}</SCPDURL><controlURL>{CONTROL_PATH}</controlURL>'
      f'<eventSubURL>/upnp/events</eventSubURL></service></serviceList>'
      f'</device></deviceList></device></deviceList></device></root>').encode("utf-8")


def service_description() -> bytes:
    """Declare action arguments for clients that inspect the IGD SCPD before SOAP."""
    actions = {
        "AddPortMapping": (("NewRemoteHost", "in", "RemoteHost"),
            ("NewExternalPort", "in", "ExternalPort"),
            ("NewProtocol", "in", "PortMappingProtocol"),
            ("NewInternalPort", "in", "InternalPort"),
            ("NewInternalClient", "in", "InternalClient"),
            ("NewEnabled", "in", "PortMappingEnabled"),
            ("NewPortMappingDescription", "in", "PortMappingDescription"),
            ("NewLeaseDuration", "in", "PortMappingLeaseDuration")),
        "DeletePortMapping": (("NewRemoteHost", "in", "RemoteHost"),
            ("NewExternalPort", "in", "ExternalPort"),
            ("NewProtocol", "in", "PortMappingProtocol")),
        "GetSpecificPortMappingEntry": (("NewRemoteHost", "in", "RemoteHost"),
            ("NewExternalPort", "in", "ExternalPort"),
            ("NewProtocol", "in", "PortMappingProtocol"),
            ("NewInternalPort", "out", "InternalPort"),
            ("NewInternalClient", "out", "InternalClient"),
            ("NewEnabled", "out", "PortMappingEnabled"),
            ("NewPortMappingDescription", "out", "PortMappingDescription"),
            ("NewLeaseDuration", "out", "PortMappingLeaseDuration")),
        "GetExternalIPAddress": (("NewExternalIPAddress", "out", "ExternalIPAddress"),),
        "GetStatusInfo": (("NewConnectionStatus", "out", "ConnectionStatus"),
            ("NewLastConnectionError", "out", "LastConnectionError"),
            ("NewUptime", "out", "Uptime")),
    }
    types = {"RemoteHost": "string", "ExternalPort": "ui2", "PortMappingProtocol": "string",
        "InternalPort": "ui2", "InternalClient": "string", "PortMappingEnabled": "boolean",
        "PortMappingDescription": "string", "PortMappingLeaseDuration": "ui4",
        "ExternalIPAddress": "string", "ConnectionStatus": "string",
        "LastConnectionError": "string", "Uptime": "ui4"}
    def action_xml(name, args):
        return (f"<action><name>{name}</name><argumentList>" +
            "".join(f"<argument><name>{arg}</name><direction>{direction}</direction>"
                    f"<relatedStateVariable>{variable}</relatedStateVariable></argument>"
                    for arg, direction, variable in args) +
            "</argumentList></action>")
    states = "".join(f'<stateVariable sendEvents="no"><name>{name}</name>'
                     f'<dataType>{datatype}</dataType></stateVariable>'
                     for name, datatype in types.items())
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
            '<specVersion><major>1</major><minor>0</minor></specVersion>'
            '<actionList>' + ''.join(action_xml(name, args)
                                    for name, args in actions.items()) + '</actionList>'
            '<serviceStateTable>' + states + '</serviceStateTable></scpd>').encode('utf-8')


def _xml_text(value) -> str:
    if value is None or type(value) is not str or len(value) > 512:
        raise SoapFault(402, "Invalid Args")
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
        if str(port) != value or not 1024 <= port <= 65535:
            raise ValueError
        return port
    except (ValueError, TypeError):
        raise SoapFault(402, "Invalid Args") from None


def parse_soap(raw: bytes, action_header: str) -> tuple[str, dict]:
    if (not raw or len(raw) > MAX_SOAP_BYTES or b"<!DOCTYPE" in raw.upper()
            or b"<!ENTITY" in raw.upper()):
        raise SoapFault(402, "Invalid Args")
    prefix = WAN_IP + "#"
    action_header = action_header.strip().strip('"')
    if not action_header.startswith(prefix):
        raise SoapFault(401, "Invalid Action")
    action = action_header[len(prefix):]
    if action not in {"AddPortMapping", "DeletePortMapping", "GetSpecificPortMappingEntry",
                      "GetExternalIPAddress", "GetStatusInfo"}:
        raise SoapFault(401, "Invalid Action")
    try:
        root = ET.fromstring(raw)
        body = root.find(f"{{{SOAP_ENV}}}Body")
        if body is None or len(body) != 1 or body[0].tag != f"{{{WAN_IP}}}{action}":
            raise SoapFault(402, "Invalid Args")
        items = {}
        for child in body[0]:
            key = child.tag.rsplit("}", 1)[-1]
            if key in items or len(key) > 64:
                raise SoapFault(402, "Invalid Args")
            items[key] = _xml_text(child.text or "")
        return action, items
    except (ET.ParseError, RecursionError):
        raise SoapFault(402, "Invalid Args") from None


def soap_envelope(action: str, values: dict) -> bytes:
    arguments = "".join(f"<{key}>{escape(str(value))}</{key}>" for key, value in values.items())
    return (f'<?xml version="1.0"?>'
            f'<s:Envelope xmlns:s="{SOAP_ENV}" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action}Response xmlns:u="{WAN_IP}">{arguments}'
            f'</u:{action}Response></s:Body></s:Envelope>').encode("utf-8")


def soap_error(error: SoapFault) -> bytes:
    return (f'<?xml version="1.0"?>'
            f'<s:Envelope xmlns:s="{SOAP_ENV}"><s:Body><s:Fault>'
            '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>'
            '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            f'<errorCode>{error.code}</errorCode><errorDescription>{error.description}</errorDescription>'
            '</UPnPError></detail></s:Fault></s:Body></s:Envelope>').encode("utf-8")


class VirtualGateway:
    def __init__(self, sandbox_ip: str, client: BridgeClient, *, http_port=48879):
        self.sandbox_ip, self.client, self.http_port = valid_ipv4(sandbox_ip), client, valid_port(http_port)
        self.udn = str(uuid.uuid5(uuid.NAMESPACE_DNS, "syn-wsl-virtual-igd:" + self.sandbox_ip))

    def dispatch(self, action: str, values: dict) -> dict:
        if action in ("GetExternalIPAddress", "GetStatusInfo"):
            if values:
                raise SoapFault(402, "Invalid Args")
            if action == "GetStatusInfo":
                return {"NewConnectionStatus": "Connected", "NewLastConnectionError": "ERROR_NONE", "NewUptime": 0}
            return {"NewExternalIPAddress": self.client.call({"action": "external-ip"})["externalIp"]}
        if values.get("NewRemoteHost", "") not in ("",):
            raise SoapFault(402, "Invalid Args")
        if set(values) != ({"NewRemoteHost", "NewExternalPort", "NewProtocol", "NewInternalPort",
                           "NewInternalClient", "NewEnabled", "NewPortMappingDescription", "NewLeaseDuration"}
                          if action == "AddPortMapping" else
                          {"NewRemoteHost", "NewExternalPort", "NewProtocol"}):
            raise SoapFault(402, "Invalid Args")
        protocol = values["NewProtocol"].upper()
        if protocol not in ("TCP", "UDP"):
            raise SoapFault(402, "Invalid Args")
        external = _port(values["NewExternalPort"])
        op = {"action": {"DeletePortMapping": "delete", "GetSpecificPortMappingEntry": "query",
                         "AddPortMapping": "map"}[action],
              "externalPort": external, "protocol": protocol}
        if action == "AddPortMapping":
            if (values["NewInternalClient"] != self.sandbox_ip or values["NewEnabled"] != "1"
                    or len(values["NewPortMappingDescription"]) > 64):
                raise SoapFault(402, "Invalid Args")
            internal = _port(values["NewInternalPort"])
            try:
                lease = int(values["NewLeaseDuration"])
                if str(lease) != values["NewLeaseDuration"] or not 0 <= lease <= 7200:
                    raise ValueError
            except ValueError:
                raise SoapFault(402, "Invalid Args") from None
            op.update(internalPort=internal, leaseSeconds=lease)
        try:
            result = self.client.call(op)
        except BridgeError:
            raise SoapFault(718 if action == "AddPortMapping" else 714, "ConflictInMappingEntry" if action == "AddPortMapping" else "NoSuchEntryInArray") from None
        if action == "GetSpecificPortMappingEntry":
            return {"NewInternalPort": result["internalPort"],
                    "NewInternalClient": result["internalClient"],
                    "NewEnabled": 1, "NewPortMappingDescription": result["description"],
                    "NewLeaseDuration": result["leaseSeconds"]}
        return {}


class VirtualIGDHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, gateway):
        self.gateway = gateway
        super().__init__((gateway.sandbox_ip, gateway.http_port), VirtualIGDHandler)


class VirtualIGDHandler(BaseHTTPRequestHandler):
    server_version = "SynVirtualIGD/1"
    sys_version = ""

    def log_message(self, *args):
        pass

    def _allowed(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", self.server.gateway.sandbox_ip}

    def _respond(self, status: int, body: bytes, mime: str):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._allowed():
            self.send_error(403)
            return
        gateway = self.server.gateway
        if self.path == ROOT_PATH:
            self._respond(200, root_description(gateway.sandbox_ip, gateway.http_port, gateway.udn), "text/xml")
        elif self.path == SERVICE_PATH:
            self._respond(200, service_description(), "text/xml")
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._allowed() or self.path != CONTROL_PATH:
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 < size <= MAX_SOAP_BYTES:
                raise SoapFault(402, "Invalid Args")
            action, values = parse_soap(self.rfile.read(size), self.headers.get("SOAPACTION", ""))
            answer = self.server.gateway.dispatch(action, values)
            status, payload = 200, soap_envelope(action, answer)
        except (SoapFault, ValueError, KeyError):
            error = sys.exc_info()[1]
            error = error if isinstance(error, SoapFault) else SoapFault(501, "ActionFailed")
            status, payload = 500, soap_error(error)
        self._respond(status, payload, 'text/xml; charset="utf-8"')


class SSDPResponder:
    def __init__(self, gateway):
        self.gateway = gateway
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("", SSDP_PORT))
            membership = socket.inet_aton(SSDP_GROUP) + socket.inet_aton(gateway.sandbox_ip)
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            self._sock.settimeout(0.4)
        except BaseException:
            self._sock.close()
            raise
        self._thread = threading.Thread(target=self._serve, daemon=True, name="syn-wsl-ssdp")
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try: raw, remote = self._sock.recvfrom(2049)
            except socket.timeout: continue
            except OSError: break
            if remote[0] not in {"127.0.0.1", self.gateway.sandbox_ip}:
                continue
            target = parse_msearch(raw)
            if target is None:
                continue
            try:
                self._sock.sendto(ssdp_reply(target, self.gateway.sandbox_ip,
                    self.gateway.http_port, self.gateway.udn), remote)
            except OSError:
                continue

    def stop(self):
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)


def serve(gateway: VirtualGateway, *, with_ssdp=True, multicast_groups=(),
          multicast_port=48778, secret=None):
    from syn_wsl_multicast_relay import MulticastRelay
    if with_ssdp and (SSDP_GROUP, SSDP_PORT) in multicast_groups:
        raise BridgeError("Raw SSDP forwarding conflicts with the virtual IGD service")
    server = ssdp = relay = relay_thread = None
    try:
        server = VirtualIGDHTTPServer(gateway)
        ssdp = SSDPResponder(gateway) if with_ssdp else None
        relay = (MulticastRelay(interface=gateway.sandbox_ip, tunnel_bind=gateway.sandbox_ip,
                 tunnel_port=multicast_port, peer_ip=gateway.client.host,
                 peer_port=multicast_port, secret=secret, groups=multicast_groups)
                 if multicast_groups else None)
        if relay:
            relay_thread = threading.Thread(target=relay.run, daemon=True,
                                             name="syn-wsl-sandbox-multicast")
            relay_thread.start()
        LOG.info("WSL virtual IGD listening at http://%s:%s%s",
                 gateway.sandbox_ip, gateway.http_port, ROOT_PATH)
        server.serve_forever(poll_interval=.25)
    finally:
        if server: server.server_close()
        if ssdp: ssdp.stop()
        if relay:
            relay.stop()
            if relay_thread: relay_thread.join(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-ip", required=True)
    parser.add_argument("--host-ip", required=True)
    parser.add_argument("--host-port", type=int, default=48777)
    parser.add_argument("--http-port", type=int, default=48879)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--without-ssdp", action="store_true")
    parser.add_argument("--mdns", action="store_true", help="Opt-in mDNS multicast relay")
    parser.add_argument("--multicast-group", action="append", default=[])
    parser.add_argument("--multicast-port", type=int, default=48778)
    args = parser.parse_args(argv)
    from syn_wsl_multicast_relay import channels
    group_list = channels(mdns=args.mdns, groups=args.multicast_group)
    secret = secret_file(args.secret)
    gateway = VirtualGateway(args.sandbox_ip, BridgeClient(args.host_ip, args.host_port,
                              secret), http_port=args.http_port)
    try:
        serve(gateway, with_ssdp=not args.without_ssdp,
              multicast_groups=group_list, multicast_port=args.multicast_port,
              secret=secret)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
