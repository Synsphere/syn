"""No router, Windows account, libp2p, or real multicast LAN is required."""
from __future__ import annotations

import http.client
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from syn_wsl_bridge_common import (BridgeClient, BridgeError, BridgePolicy, ReplayGuard,
    canonical, secret_file, sign_request)
from syn_wsl_host_bridge import (BridgeHTTPServer, BridgeMappings, MiniUpnpGateway,
    TcpIngress, UdpIngress)
from syn_wsl_multicast_relay import (MDNS, SSDP, BridgeError as MulticastError,
    FrameVerifier, channel, channel_peer, channels, seal)
from syn_wsl_sandbox_igd import (WAN_IP, VirtualGateway, parse_msearch, parse_soap,
    soap_envelope, SoapFault)

SECRET = bytes(range(32))
WINDOWS_LAN = "127.0.0.2"
WINDOWS_WSL = "127.0.0.3"
WSL = "127.0.0.1"


class FakeIGD:
    lanaddr = WINDOWS_LAN
    def __init__(self):
        self.rows = {}
        self.selected = False
        self.fail_next = False
    def discover(self): return 1
    def selectigd(self): self.selected = True
    def externalipaddress(self): return "198.51.100.27"
    def getspecificportmapping(self, port, protocol):
        item = self.rows.get((port, protocol))
        return (item["client"], item["port"], item["description"]) if item else None
    def addportmapping(self, port, protocol, client, internal, description, _, lease):
        if self.fail_next:
            self.fail_next = False
            return False
        self.rows[port, protocol] = {"client": client, "port": internal,
                                     "description": description}
        return True
    def deleteportmapping(self, port, protocol):
        self.rows.pop((port, protocol), None)
        return True


class StubIngress:
    made = []
    def __init__(self, *args):
        self.args = args
        self.stopped = False
        self.made.append(self)
    def stop(self): self.stopped = True


def policy():
    return BridgePolicy(WINDOWS_LAN, WINDOWS_WSL, WSL, 42000, 42010,
                        max_mappings=3, max_lease_seconds=120)


def setup_manager(now=None):
    router = MiniUpnpGateway(WINDOWS_LAN, upnp=FakeIGD())
    return BridgeMappings(policy(), router, tcp_factory=StubIngress,
                          udp_factory=StubIngress, now=now or time.monotonic)


def test_bridge_hmac_freshness_integrity_and_replay():
    guard = ReplayGuard()
    packet, tag = sign_request(SECRET, {"action":"external-ip"},
                               now=1_800_000_000, nonce="b"*32)
    assert guard.verify(SECRET, packet, tag, now=1_800_000_000) == {"action":"external-ip"}
    with pytest.raises(BridgeError, match="Repeated"):
        guard.verify(SECRET, packet, tag, now=1_800_000_000)
    with pytest.raises(BridgeError, match="authentication"):
        ReplayGuard().verify(SECRET, packet+b"\x00", tag, now=1_800_000_000)
    with pytest.raises(BridgeError, match="expired"):
        ReplayGuard().verify(SECRET, packet, tag, now=1_800_000_050)


def test_bridge_secret_rejects_public_file_and_symlink(tmp_path):
    path = tmp_path/"secret"
    path.write_text(SECRET.hex())
    path.chmod(0o644)
    if sys.platform != "win32":
        with pytest.raises(BridgeError, match="0600"):
            secret_file(path)
    path.chmod(0o600)
    assert secret_file(path) == SECRET
    linked = tmp_path/"link"
    linked.symlink_to(path)
    with pytest.raises(BridgeError, match="non-symlink"):
        secret_file(linked)


def test_port_policy_rejects_unapproved_range_and_credentials():
    config = policy()
    assert config.port(42010) == 42010
    with pytest.raises(BridgeError): config.port(4001)
    with pytest.raises(BridgeError): config.protocol("ICMP")
    with pytest.raises(BridgeError): BridgePolicy(WINDOWS_LAN, WINDOWS_LAN, WSL)
    with pytest.raises(BridgeError): BridgePolicy(WINDOWS_LAN, WINDOWS_WSL, WSL, 4000, 8000)


def test_gateway_readback_exact_and_foreign_ownership():
    igd = FakeIGD()
    gateway = MiniUpnpGateway(WINDOWS_LAN, upnp=igd)
    assert igd.selected
    gateway.add(42001, "UDP", "Syn WSL sandbox UDP 42001", 120)
    assert gateway.get(42001, "UDP")["description"] == "Syn WSL sandbox UDP 42001"
    with pytest.raises(BridgeError, match="ownership"):
        gateway.remove(42001, "UDP", "Unrelated owner")
    assert gateway.get(42001, "UDP") is not None
    gateway.remove(42001, "UDP", "Syn WSL sandbox UDP 42001")
    assert gateway.get(42001, "UDP") is None


def test_mapping_owner_conflicts_lease_and_cleanup():
    StubIngress.made.clear()
    counter = [100.0]
    manager = setup_manager(lambda: counter[0])
    mapped = manager.apply("UDP", 42000, 41000, 60)
    assert mapped["mappingVerified"] and mapped["leaseSeconds"] == 60
    assert mapped["internalClient"] == WSL
    assert mapped["publicReachability"] == "unverified"
    assert len(StubIngress.made) == 1
    assert manager.apply("UDP", 42000, 41000, 0)["leaseSeconds"] == 120
    assert len(StubIngress.made) == 1
    with pytest.raises(BridgeError, match="targets another"):
        manager.apply("UDP", 42000, 41001, 60)
    with pytest.raises(BridgeError, match="not owned"):
        manager.remove("TCP", 42001)
    manager.apply("TCP", 42001, 41001, 60)
    assert len(StubIngress.made) == 2
    counter[0] += 121
    manager.sweep()
    assert manager.status()["mappings"] == []
    assert all(row.stopped for row in StubIngress.made)


def test_mapping_router_collision_and_failure_releases_proxy():
    StubIngress.made.clear()
    manager = setup_manager()
    igd = manager.router.upnp
    igd.rows[(42000, "UDP")] = {"client": WINDOWS_LAN, "port": 42000,
                                  "description": "Someone else's mapping"}
    with pytest.raises(BridgeError, match="owned by another"):
        manager.apply("UDP", 42000, 41000, 60)
    assert not StubIngress.made
    igd.rows.clear()
    igd.fail_next = True
    with pytest.raises(BridgeError, match="rejected"):
        manager.apply("UDP", 42000, 41000, 60)
    assert StubIngress.made[-1].stopped
    assert not manager.status()["mappings"]


def test_control_http_authentication_end_to_end():
    manager = setup_manager()
    server = BridgeHTTPServer(manager, SECRET, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = BridgeClient(WINDOWS_WSL, server.server_port, SECRET)
        assert client.call({"action":"external-ip"})["mappingVerified"] is False
        result = client.call({"action":"map", "protocol":"TCP", "externalPort":42000,
                              "internalPort":41000, "leaseSeconds":60})
        assert result["externalPort"] == 42000
        assert client.call({"action":"query", "protocol":"TCP", "externalPort":42000})["internalPort"] == 41000
        client.call({"action":"delete", "protocol":"TCP", "externalPort":42000})
        with pytest.raises(BridgeError):
            client.call({"action":"delete", "protocol":"TCP", "externalPort":42000})
        with pytest.raises(BridgeError):
            BridgeClient(WINDOWS_WSL, server.server_port, b"wrong"*8).call({"action":"external-ip"})
    finally:
        server.shutdown()
        server.server_close()
        manager.close()
        thread.join(timeout=1)


def _free_port(typ, bind="127.0.0.2"):
    with socket.socket(socket.AF_INET, typ) as sock:
        sock.bind((bind, 0))
        return sock.getsockname()[1]


def test_udp_proxy_retains_independent_return_paths_for_two_clients():
    target = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target.bind((WSL, 0))
    target.settimeout(2)
    internal = target.getsockname()[1]
    external = _free_port(socket.SOCK_DGRAM)
    proxy = UdpIngress(WINDOWS_LAN, external, WINDOWS_WSL, WSL, internal)
    clients = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
    for client in clients: client.settimeout(3)
    try:
        for index, client in enumerate(clients):
            client.sendto(f"request-{index}".encode(), (WINDOWS_LAN, external))
        incoming = [target.recvfrom(64) for _ in clients]
        assert len({source for _, source in incoming}) == 2
        for packet, source in incoming:
            target.sendto(b"reply:"+packet, source)
        replies = [client.recvfrom(128) for client in clients]
        assert replies[0] == (b"reply:request-0", (WINDOWS_LAN, external))
        assert replies[1] == (b"reply:request-1", (WINDOWS_LAN, external))
    finally:
        for s in clients: s.close()
        proxy.stop()
        target.close()


def test_tcp_proxy_forwards_bytes_without_persistent_netsh_rule():
    backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    backend.bind((WSL, 0)); backend.listen(2); backend.settimeout(3)
    internal = backend.getsockname()[1]
    external = _free_port(socket.SOCK_STREAM)
    relay = TcpIngress(WINDOWS_LAN, external, WSL, internal)
    def answer():
        connection, _ = backend.accept()
        with connection:
            received = connection.recv(32)
            connection.sendall(b"echo:"+received)
    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        with socket.create_connection((WINDOWS_LAN, external), timeout=3) as client:
            client.sendall(b"hello")
            assert client.recv(64) == b"echo:hello"
        thread.join(timeout=2)
    finally:
        relay.stop()
        backend.close()


def test_ssdp_parse_and_virtual_igd_respects_internal_client():
    packet = (b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
              b'MAN: "ssdp:discover"\r\nMX: 1\r\nST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n')
    assert parse_msearch(packet) == "urn:schemas-upnp-org:device:internetgatewaydevice:1"
    assert parse_msearch(packet.replace(b'M-SEARCH', b'NOTIFY',1)) is None
    assert parse_msearch(packet*70) is None
    captured = []
    class Client:
        def call(self, op):
            captured.append(op)
            if op["action"] == "external-ip": return {"externalIp": "198.51.100.27"}
            return {"internalClient": WSL, "internalPort": op.get("internalPort", 41000),
                    "description": "Owned by WSL", "leaseSeconds": 60}
    bridge = VirtualGateway(WSL, Client())
    values = {"NewRemoteHost":"", "NewExternalPort":"42000", "NewProtocol":"UDP",
              "NewInternalPort":"41000", "NewInternalClient":WSL, "NewEnabled":"1",
              "NewPortMappingDescription":"Test", "NewLeaseDuration":"60"}
    assert bridge.dispatch("AddPortMapping", values) == {}
    assert captured[-1]["action"] == "map"
    assert bridge.dispatch("GetExternalIPAddress", {})["NewExternalIPAddress"] == "198.51.100.27"
    with pytest.raises(SoapFault, match="Invalid Args"):
        bridge.dispatch("AddPortMapping", {**values, "NewInternalClient": "192.168.1.200"})


def test_soap_xml_parser_rejects_entities_and_action_mismatch():
    xml = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
           f'<s:Body><u:GetExternalIPAddress xmlns:u="{WAN_IP}"/></s:Body></s:Envelope>').encode()
    assert parse_soap(xml, WAN_IP+"#GetExternalIPAddress") == ("GetExternalIPAddress", {})
    with pytest.raises(SoapFault): parse_soap(xml, WAN_IP+"#DeletePortMapping")
    with pytest.raises(SoapFault): parse_soap(b'<!DOCTYPE foo [<!ENTITY x "bad">]>'+xml, WAN_IP+"#GetExternalIPAddress")
    assert b"GetExternalIPAddressResponse" in soap_envelope("GetExternalIPAddress", {"NewExternalIPAddress":"203.0.113.10"})


def test_multicast_is_opt_in_bounded_authenticated_and_replay_safe():
    assert channels() == ()
    assert channels(mdns=True) == (MDNS,)
    assert channels(groups=["239.255.255.250:1900"]) == (SSDP,)
    for value in ("224.0.0.1:5353", "239.255.255.255:0", "192.168.1.1:5353", "bad"):
        with pytest.raises(BridgeError): channel(value)
    assert channel_peer("127.0.0.2:48778") == ("127.0.0.2", 48778)
    packet = seal(SECRET, *MDNS, b"query", timestamp=1_800_000_000, nonce=b"12345678")
    verifier = FrameVerifier(SECRET, channels(mdns=True))
    assert verifier.open(packet, now=1_800_000_000) == (MDNS, b"query")
    with pytest.raises(BridgeError, match="replay"):
        verifier.open(packet, now=1_800_000_000)
    with pytest.raises(BridgeError, match="authentication"):
        FrameVerifier(SECRET, channels(mdns=True)).open(packet[:-1] + b"!", now=1_800_000_000)
    with pytest.raises(BridgeError, match="Unapproved"):
        FrameVerifier(SECRET, channels(groups=["239.255.255.250:1900"])).open(packet, now=1_800_000_000)
    with pytest.raises(BridgeError, match="stale"):
        FrameVerifier(SECRET, channels(mdns=True)).open(packet, now=1_800_000_099)
