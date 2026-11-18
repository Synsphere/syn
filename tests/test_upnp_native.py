"""Exercise the installed C binding against a loopback-only SOAP gateway.

SSDP is replaced with a known description URL, and the reported LAN address
is a fixture value. Selection, external-address queries and mapping operations
otherwise use real miniupnpc calls, including its error-masking lookup behavior.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import miniupnpc
import pytest
import trio

from syn.upnp import SynUpnpManager


SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
CONTROL = "urn:schemas-upnp-org:control-1-0"
LAN = "192.168.1.10"
WAN = "93.184.216.34"
CONTROL_PATH = "/upnp?control=Connection1"
SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
)


class _Igd:
    def __init__(self, service):
        self.service = service
        self.table = {}
        self.calls = []
        self.lookup_fault = None
        self.permanent_only = False

    def description(self):
        return (f'''<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <device>
  <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
  <friendlyName>Loopback test gateway</friendlyName><manufacturer>Syn tests</manufacturer>
  <modelName>Fixture</modelName><UDN>uuid:syn-upnp-native-test</UDN>
  <deviceList><device>
   <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
   <serviceList><service>
    <serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>
    <serviceId>urn:upnp-org:serviceId:WANCommonIFC1</serviceId>
    <SCPDURL>/common.xml</SCPDURL><controlURL>/common</controlURL><eventSubURL>/common-event</eventSubURL>
   </service></serviceList>
   <deviceList><device>
    <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
    <serviceList><service>
     <serviceType>{self.service}</serviceType>
     <serviceId>urn:upnp-org:serviceId:Connection1</serviceId>
     <SCPDURL>/connection.xml</SCPDURL><controlURL>{CONTROL_PATH}</controlURL>
     <eventSubURL>/connection-event</eventSubURL>
    </service></serviceList>
   </device></deviceList>
  </device></deviceList>
 </device>
</root>''').encode()

    def response(self, action, values):
        fields = "".join(f"<{key}>{escape(str(value))}</{key}>" for key, value in values.items())
        return (f'<s:Envelope xmlns:s="{SOAP}"><s:Body>'
                f'<u:{action}Response xmlns:u="{self.service}">{fields}</u:{action}Response>'
                '</s:Body></s:Envelope>').encode()

    @staticmethod
    def fault(code):
        description = {401: "Invalid Action", 501: "Action Failed", 606: "Action not authorized",
                       714: "NoSuchEntryInArray", 725: "OnlyPermanentLeasesSupported"}[code]
        return (f'<s:Envelope xmlns:s="{SOAP}"><s:Body><s:Fault>'
                '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>'
                f'<UPnPError xmlns="{CONTROL}"><errorCode>{code}</errorCode>'
                f'<errorDescription>{description}</errorDescription></UPnPError>'
                '</detail></s:Fault></s:Body></s:Envelope>').encode()

    def request(self, path, service, action, values):
        self.calls.append((path, service, action, values))
        if path != CONTROL_PATH or service != self.service:
            return 500, self.fault(401)
        if action == "GetExternalIPAddress":
            return 200, self.response(action, {"NewExternalIPAddress": WAN})
        if action == "GetStatusInfo":
            return 200, self.response(action, {"NewConnectionStatus": "Connected",
                "NewLastConnectionError": "ERROR_NONE", "NewUptime": 60})
        if action not in {"GetSpecificPortMappingEntry", "AddPortMapping", "DeletePortMapping"}:
            # Count and generic enumeration deliberately are not implemented.
            return 500, self.fault(401)
        key = int(values["NewExternalPort"]), values["NewProtocol"]
        if action == "GetSpecificPortMappingEntry":
            if self.lookup_fault is not None:
                return 500, self.fault(self.lookup_fault)
            if key not in self.table:
                return 500, self.fault(714)
            return 200, self.response(action, self.table[key])
        if action == "AddPortMapping":
            if self.permanent_only and int(values["NewLeaseDuration"]):
                return 500, self.fault(725)
            self.table[key] = {name: values[name] for name in (
                "NewInternalPort", "NewInternalClient", "NewEnabled",
                "NewPortMappingDescription", "NewLeaseDuration")}
            return 200, self.response(action, {})
        self.table.pop(key, None)
        return 200, self.response(action, {})


@contextmanager
def loopback_igd(service=SERVICES[0]):
    igd = _Igd(service)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", 'text/xml; charset="utf-8"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200, igd.description())

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = ET.fromstring(raw).find(f"{{{SOAP}}}Body")
            request = body[0]
            service, action = request.tag[1:].split("}", 1)
            values = {field.tag.rsplit("}", 1)[-1]: field.text or "" for field in request}
            self.reply(*igd.request(self.path, service, action, values))

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=.01),
                    name="syn-test-upnp-http")
    thread.start()
    igd.description_url = f"http://127.0.0.1:{server.server_port}/root.xml"
    try:
        yield igd
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "Loopback gateway thread did not stop"


class _NativeGateway:
    __slots__ = ("native", "description_url")

    def __init__(self, description_url, **kwargs):
        self.native = miniupnpc.UPnP(**kwargs)
        self.description_url = description_url

    def __getattr__(self, name):
        return getattr(self.native, name)

    @property
    def lanaddr(self):
        # The fixture is loopback-only; never contact this synthetic LAN IP.
        return LAN

    def discover(self):
        return 1

    def selectigd(self):
        return self.native.selectigd(self.description_url)


def native_manager(igd):
    created = []

    def factory(**kwargs):
        gateway = _NativeGateway(igd.description_url, **kwargs)
        created.append(gateway)
        return gateway

    return SynUpnpManager(multicast_interface=LAN, discover_delay_ms=1500,
        gateway_factory=factory,
        environment_probe=lambda: {"platform": "linux", "wsl": False, "networkingMode": None}), created


@pytest.mark.parametrize("service_type", SERVICES)
def test_native_binding_maps_without_optional_count_action(service_type):
    with loopback_igd(service_type) as igd:
        async def exercise():
            manager, created = native_manager(igd)
            assert await manager.discover(), manager.status()
            native = created[0].native
            assert native.multicastif == LAN and native.discoverdelay == 1500
            with pytest.raises((TypeError, AttributeError)):
                native.multicastif = LAN
            assert native.getspecificportmapping(4001, "TCP") is None
            with pytest.raises(Exception):
                native.getportmappingnumberofentries()
            assert await manager.add_port_mapping(4001), manager.status()
            assert manager.status()["mappings"][0]["verified"]
            assert igd.table[(4001, "TCP")]["NewInternalClient"] == LAN
            assert await manager.remove_port_mapping(4001), manager.status()
            assert not igd.table and not manager.status()["mappings"]
            actions = [call[2] for call in igd.calls]
            assert actions.count("GetPortMappingNumberOfEntries") == 1  # Only the explicit native probe.
            assert "GetGenericPortMappingEntry" not in actions
            assert actions.count("AddPortMapping") == actions.count("DeletePortMapping") == 1
        trio.run(exercise)


@pytest.mark.parametrize("fault", [501, 606])
def test_native_masked_lookup_fault_does_not_authorize_a_write(fault):
    with loopback_igd() as igd:
        igd.lookup_fault = fault
        async def exercise():
            manager, created = native_manager(igd)
            assert await manager.discover(), manager.status()
            assert created[0].native.getspecificportmapping(4001, "TCP") is None
            assert not await manager.add_port_mapping(4001)
            assert not manager.status()["mappings"]
            assert not any(call[2] in {"AddPortMapping", "DeletePortMapping"} for call in igd.calls)
        trio.run(exercise)


def test_native_cleanup_retains_ownership_when_lookup_is_inconclusive():
    with loopback_igd() as igd:
        async def exercise():
            manager, _ = native_manager(igd)
            assert await manager.discover(), manager.status()
            assert await manager.add_port_mapping(4001), manager.status()
            igd.lookup_fault = 606
            assert not await manager.remove_port_mapping(4001)
            assert len(manager.status()["mappings"]) == 1 and (4001, "TCP") in igd.table
            assert not any(call[2] == "DeletePortMapping" for call in igd.calls)
            igd.lookup_fault = None
            assert await manager.remove_port_mapping(4001), manager.status()
            assert not manager.status()["mappings"] and not igd.table
        trio.run(exercise)


def test_native_permanent_lease_error_retries_with_zero_lease():
    with loopback_igd() as igd:
        igd.permanent_only = True
        async def exercise():
            manager, _ = native_manager(igd)
            assert await manager.discover(), manager.status()
            assert await manager.add_port_mapping(4001), manager.status()
            leases = [call[3]["NewLeaseDuration"] for call in igd.calls if call[2] == "AddPortMapping"]
            assert leases == ["3600", "0"]
            assert manager.status()["mappings"][0]["leaseSeconds"] == 0
            assert await manager.remove_port_mapping(4001), manager.status()
        trio.run(exercise)
