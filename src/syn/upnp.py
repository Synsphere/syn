"""Verified, owned UPnP mappings without blocking libp2p host readiness.

Discovery and router acknowledgements are evidence of a mapping, not proof that
an Internet peer can reach a listener. Native SOAP calls are serialized and are
not abandoned on cancellation: cleanup must not race an unfinished router write.
"""
from __future__ import annotations

from copy import deepcopy
import http.client
import ipaddress
import os
import platform
import re
import secrets
import subprocess
import sys
from urllib.parse import urlsplit
from xml.etree import ElementTree

import miniupnpc
import trio

MAX_ROUTER_MAPPINGS = 128
MAX_OWNED_MAPPINGS = 32
LEASE_SECONDS = 3600
SOAP_TIMEOUT_SECONDS = 3
MAX_SOAP_BYTES = 64 * 1024
SOAP_ENVELOPE = "http://schemas.xmlsoap.org/soap/envelope/"
UPNP_CONTROL = "urn:schemas-upnp-org:control-1-0"
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
)


def network_environment():
    """Read WSL's actual networking mode without modifying host configuration."""
    wsl = sys.platform == "linux" and (
        "microsoft" in platform.release().lower() or "wsl" in platform.release().lower()
        or bool(os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME")))
    result = {"platform": sys.platform, "wsl": wsl, "networkingMode": None}
    if not wsl:
        return result
    result["networkingMode"] = "unknown"
    try:
        reply = subprocess.run(["wslinfo", "--networking-mode"], capture_output=True,
                               text=True, timeout=2, check=False)
        mode = reply.stdout.strip().lower()
        if reply.returncode == 0 and mode in {"nat", "mirrored", "virtioproxy", "bridged", "none"}:
            result["networkingMode"] = mode
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    return result


def _port(port, protocol):
    if (type(port) is not int or not 1 <= port <= 65535
            or type(protocol) is not str or protocol not in {"TCP", "UDP"}):
        raise ValueError("UPnP requires a valid integer port and TCP or UDP")
    return port, protocol


def _ipv4(value, *, public=False):
    if type(value) is not str or len(value) > 64:
        raise ValueError("Expected an IPv4 address string")
    address = ipaddress.ip_address(value)
    if (address.version != 4 or address.is_unspecified or address.is_multicast
            or address.is_loopback or address.is_reserved
            or (public and not address.is_global)):
        raise ValueError("Router did not supply a usable IPv4 address")
    return str(address)


def _mapping(value, *, remote=""):
    if not isinstance(value, (tuple, list)) or len(value) != 5:
        raise ValueError("Invalid router mapping response")
    host, port, description, enabled, lease = value
    if (type(host) is not str or len(host) > 64 or type(port) is not int
            or not 1 <= port <= 65535 or type(description) is not str or len(description) > 80
            or type(enabled) is not bool or type(lease) is not int or not 0 <= lease <= 0xFFFFFFFF):
        raise ValueError("Invalid router mapping fields")
    return {"internalAddress": str(ipaddress.ip_address(host)), "internalPort": port,
            "description": description, "enabled": enabled, "leaseSeconds": lease,
            "remoteHost": remote}


class _SoapTreeBuilder(ElementTree.TreeBuilder):
    def doctype(self, name, public_id, system_id):
        # A byte substring check alone misses UTF-16/UTF-32 declarations.
        raise ValueError("SOAP responses must not contain a document type")


class _SpecificMappingQuery:
    """Read-only fallback for the error-masking miniupnpc Python lookup.

    The binding exposes the selected control URL, but not its service type.
    Probe the standard WAN service namespaces using only GetSpecific; cache
    the first accepted namespace. Fault 714 is the sole absence evidence.
    """

    def __init__(self, control_url):
        target = urlsplit(control_url)
        if (target.scheme not in {"http", "https"} or not target.hostname
                or target.username is not None or target.password is not None
                or target.fragment):
            raise ValueError("The selected gateway has an invalid control URL")
        self._target = target
        self._service = None

    def _request(self, service, port, protocol):
        action = "GetSpecificPortMappingEntry"
        payload = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<s:Envelope xmlns:s="{SOAP_ENVELOPE}" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service}"><NewRemoteHost></NewRemoteHost>'
            f'<NewExternalPort>{port}</NewExternalPort><NewProtocol>{protocol}</NewProtocol>'
            f'</u:{action}></s:Body></s:Envelope>'
        ).encode("ascii")
        target = self._target
        # HTTPConnection does not inherit proxy settings or follow redirects.
        connection_class = (http.client.HTTPSConnection if target.scheme == "https"
                            else http.client.HTTPConnection)
        connection = connection_class(target.hostname, target.port, timeout=SOAP_TIMEOUT_SECONDS)
        try:
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            connection.request("POST", path, payload, {
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service}#{action}"',
                "Connection": "close",
            })
            reply = connection.getresponse()
            raw = reply.read(MAX_SOAP_BYTES + 1)
            if (reply.status not in {200, 500} or len(raw) > MAX_SOAP_BYTES
                    or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper()):
                raise ValueError("The router's SOAP lookup could not be verified")
            root = ElementTree.fromstring(raw,
                parser=ElementTree.XMLParser(target=_SoapTreeBuilder()))
            if root.tag != f"{{{SOAP_ENVELOPE}}}Envelope":
                raise ValueError("Invalid SOAP lookup envelope")
            body = root.find(f"{{{SOAP_ENVELOPE}}}Body")
            if body is None or len(body) != 1:
                raise ValueError("Invalid SOAP lookup body")
            result = body[0]
            if result.tag == f"{{{SOAP_ENVELOPE}}}Fault" and reply.status == 500:
                error = result.find(f".//{{{UPNP_CONTROL}}}UPnPError")
                codes = [] if error is None else [node.text for node in error
                    if node.tag in {"errorCode", f"{{{UPNP_CONTROL}}}errorCode"}]
                if len(codes) == 1 and codes[0] in {"401", "714"}:
                    return int(codes[0]), None
                raise ValueError("The router did not confirm the requested mapping state")
            if reply.status != 200 or result.tag != f"{{{service}}}{action}Response":
                raise ValueError("Unexpected SOAP lookup response")
            values = {}
            for node in result:
                name = node.tag.removeprefix(f"{{{service}}}")
                if name in values:
                    raise ValueError("Duplicate SOAP mapping field")
                values[name] = node.text or ""
            if values.get("NewEnabled") not in {"0", "1"}:
                raise ValueError("Invalid SOAP mapping enabled flag")
            value = _mapping((values["NewInternalClient"], int(values["NewInternalPort"]),
                values["NewPortMappingDescription"], values["NewEnabled"] == "1",
                int(values["NewLeaseDuration"])))
            return 0, value
        finally:
            connection.close()

    def __call__(self, port, protocol):
        _port(port, protocol)
        services = (self._service,) if self._service else WAN_SERVICES
        for service in services:
            code, value = self._request(service, port, protocol)
            if code != 401:
                self._service = service
                return value
        raise ValueError("No standard WAN mapping query was accepted")


def _lookup(gateway, port, protocol, query=None):
    value = gateway.getspecificportmapping(port, protocol)
    if value is not None:
        return _mapping(value)
    if query is not None:
        # Unlike the Python binding, the direct read-only query distinguishes
        # a missing entry (714) from transport, authorization and SOAP errors.
        return query(port, protocol)
    # miniupnpc's Python binding returns None for failed GetSpecific *and*
    # absent mappings. GetGeneric also masks errors. Confirm absence using a
    # successful count plus every entry; a truncated/failed enumeration is not
    # permission to overwrite a slot or declare deletion complete.
    count = gateway.getportmappingnumberofentries()
    if type(count) is not int or not 0 <= count <= MAX_ROUTER_MAPPINGS:
        raise ValueError("Router mapping table cannot be verified within its bound")
    found = None
    for index in range(count):
        row = gateway.getgenericportmapping(index)
        if not isinstance(row, (tuple, list)) or len(row) != 7:
            raise ValueError("Router mapping enumeration was incomplete")
        external, kind, target, description, enabled, remote, lease = row
        _port(external, kind)
        if external == port and kind == protocol:
            if (not isinstance(target, (tuple, list)) or len(target) != 2
                    or enabled not in {"0", "1"} or type(remote) is not str or len(remote) > 64):
                raise ValueError("Invalid enumerated router mapping")
            found = _mapping((*target, description, enabled == "1", lease), remote=remote)
    final_count = gateway.getportmappingnumberofentries()
    if type(final_count) is not int or final_count != count:
        raise ValueError("Router mapping table changed during verification")
    return found


class SynUpnpManager:
    def __init__(self, multicast_interface=None, discover_delay_ms=2000, *,
                 gateway_factory=None, environment_probe=None):
        if (type(discover_delay_ms) is not int or not 100 <= discover_delay_ms <= 10000):
            raise ValueError("UPnP discovery delay must be 100–10000 milliseconds")
        if multicast_interface is not None:
            multicast_interface = _ipv4(multicast_interface)
        self._factory = gateway_factory or miniupnpc.UPnP
        self._probe = environment_probe or network_environment
        self._interface, self._delay = multicast_interface, discover_delay_ms
        self._gateway = None
        self._control_url = self._query = None
        self._lan_address = self._external_ip = None
        self._owned = {}
        self._description = "Syn " + secrets.token_hex(16)
        self._lock = trio.Lock()
        self._limiter = trio.CapacityLimiter(1)
        self._state = {"enabled": True, "state": "waiting", "reason": None,
                       "devices": None, "lanAddress": None, "externalIp": None,
                       "multicastInterface": self._interface, "discoverDelayMs": self._delay,
                       "environment": None, "inboundReachabilityVerified": False}

    async def _run(self, action):
        # miniupnpc exposes the SSDP discovery delay but no Python-level SOAP
        # timeout. Do not claim cancellation stopped an in-flight native write.
        return await trio.to_thread.run_sync(action, abandon_on_cancel=False, limiter=self._limiter)

    def status(self):
        return {**deepcopy(self._state), "mappings": [
            {name: value[name] for name in ("port", "protocol", "internalAddress", "internalPort",
                                          "owned", "verified", "leaseSeconds")}
            for _, value in sorted(self._owned.items())]}

    def get_external_ip(self):
        return self._external_ip

    async def discover(self):
        async with self._lock:
            self._gateway = None
            self._control_url = self._query = None
            self._lan_address = self._external_ip = None
            for record in self._owned.values():
                record["verified"] = False
            self._state.update(state="discovering", reason=None, devices=None,
                               lanAddress=None, externalIp=None)

            def discover_native():
                state = {"state": "unavailable", "reason": None, "devices": None,
                         "lanAddress": None, "externalIp": None, "environment": self._probe()}
                environment = state["environment"]
                if environment.get("wsl") and environment.get("networkingMode") in {"nat", "virtioproxy", "none"}:
                    state["reason"] = ("WSL is using " + environment["networkingMode"] +
                        " networking. Router UPnP cannot by itself expose this WSL listener; "
                        "use the documented Windows-to-WSL forwarding setup or mirrored networking.")
                    return state, None
                try:
                    # multicastif is a read-only C member: it must be supplied
                    # at construction, not assigned after creating UPnP().
                    gateway = self._factory(multicastif=self._interface, discoverdelay=self._delay)
                    devices = gateway.discover()
                except Exception as error:
                    state["reason"] = ("UPnP discovery returned an inconclusive native result; no device count is known."
                        if str(error) == "Success" else "UPnP discovery failed (" + type(error).__name__ + ").")
                    return state, None
                if type(devices) is not int or devices < 0:
                    state["reason"] = "UPnP discovery returned an invalid device count."
                    return state, None
                state["devices"] = devices
                if not devices:
                    state["reason"] = "No UPnP device responded on this network interface."
                    return state, None
                try:
                    control_url = gateway.selectigd()
                except Exception:
                    state["reason"] = "UPnP discovery found devices, but an Internet gateway could not be selected."
                    return state, None
                try:
                    local = _ipv4(gateway.lanaddr)
                    external = _ipv4(gateway.externalipaddress(), public=True)
                except Exception:
                    state["reason"] = "The selected gateway did not supply a usable LAN address and public IPv4 address."
                    return state, None
                state.update(state="ready", reason=None, lanAddress=local, externalIp=external)
                return state, (gateway, control_url)

            state, selected = await self._run(discover_native)
            self._state.update(state)
            if selected is not None:
                self._gateway, self._control_url = selected
                if self._control_url is not None:
                    try:
                        self._query = _SpecificMappingQuery(self._control_url)
                    except (TypeError, ValueError):
                        self._gateway = None
                        self._state.update(state="unavailable", reason="The gateway control URL is invalid.",
                                           lanAddress=None, externalIp=None)
                        state = self._state
            self._lan_address, self._external_ip = state["lanAddress"], state["externalIp"]
            return self._gateway is not None

    @staticmethod
    def _matches(value, record):
        return value is not None and all(value.get(name) == record[name] for name in (
            "internalAddress", "internalPort", "description", "remoteHost"))

    async def add_port_mapping(self, port, protocol="TCP"):
        key = _port(port, protocol)
        async with self._lock:
            if self._gateway is None:
                return False
            if key not in self._owned and len(self._owned) >= MAX_OWNED_MAPPINGS:
                self._state.update(state="mapping-failed", reason="Syn's owned UPnP mapping limit is reached.")
                return False
            gateway, local = self._gateway, self._lan_address
            previous = self._owned.get(key)
            if previous is not None and (
                    previous["controlUrl"] != self._control_url
                    or previous["internalAddress"] != local):
                # Preserve the original handle until its mapping is either
                # removed, absent, or no longer ours. Switching gateways must
                # not orphan an earlier mapping (especially a permanent one).
                okay, release, reason = await self._run(lambda: self._remove_native(previous))
                if not release:
                    previous["verified"] = False
                    self._state.update(state="cleanup-failed", reason=reason)
                    return False
                self._owned.pop(key, None)
            record = {"port": port, "protocol": protocol, "gateway": gateway,
                      "controlUrl": self._control_url, "query": self._query,
                      "internalAddress": local, "internalPort": port, "description": self._description,
                      "remoteHost": "", "owned": True, "verified": False, "leaseSeconds": LEASE_SECONDS}

            def add_native():
                try:
                    current = _lookup(gateway, port, protocol, self._query)
                except Exception:
                    return False, False, "The router's existing port mapping could not be verified; no mapping was changed."
                if current is not None and (key not in self._owned or not self._matches(current, record)):
                    return False, False, "This router port already has another mapping; no mapping was changed."
                try:
                    try:
                        accepted = gateway.addportmapping(port, protocol, local, port, self._description, "", LEASE_SECONDS)
                    except Exception as error:
                        if "OnlyPermanentLeasesSupported" not in str(error) and not re.search(r"\b725\b", str(error)):
                            raise
                        record["leaseSeconds"] = 0
                        accepted = gateway.addportmapping(port, protocol, local, port, self._description, "", 0)
                    if accepted is not True:
                        return False, True, "The router did not acknowledge the requested UPnP mapping."
                    current = _lookup(gateway, port, protocol, self._query)
                    if not self._matches(current, record) or not current["enabled"]:
                        return False, True, "The router acknowledged a mapping, but exact enabled readback did not match."
                    record["leaseSeconds"] = current["leaseSeconds"]
                    return True, True, None
                except Exception:
                    return False, True, "The UPnP mapping write or readback failed; its outcome is not confirmed."

            okay, attempted, reason = await self._run(add_native)
            if attempted:
                record["verified"] = okay
                self._owned[key] = record
            elif key in self._owned:
                self._owned[key]["verified"] = False
            self._state.update(state="mapped" if okay else "mapping-failed", reason=reason)
            return okay

    def _remove_native(self, record):
        gateway, port, protocol = record["gateway"], record["port"], record["protocol"]
        try:
            current = _lookup(gateway, port, protocol, record["query"])
            if current is None:
                return True, True, None
            if not self._matches(current, record):
                return False, True, "The router mapping changed ownership; Syn did not delete it."
            if gateway.deleteportmapping(port, protocol) is not True:
                return False, False, "The router did not acknowledge removal of Syn's UPnP mapping."
            if _lookup(gateway, port, protocol, record["query"]) is not None:
                return False, False, "The router mapping remains present after the removal request."
            return True, True, None
        except Exception:
            return False, False, "Removal of Syn's UPnP mapping could not be verified."

    async def remove_port_mapping(self, port, protocol="TCP"):
        key = _port(port, protocol)
        async with self._lock:
            record = self._owned.get(key)
            if record is None:
                return False
            okay, release, reason = await self._run(lambda: self._remove_native(record))
            if release:
                self._owned.pop(key, None)
            else:
                record["verified"] = False
            settled = ("mapped" if any(value["verified"] for value in self._owned.values()) else "ready") if self._gateway else "unavailable"
            self._state.update(state=settled if okay else "cleanup-failed",
                               reason=(self._state["reason"] if okay and self._gateway is None else reason))
            return okay
