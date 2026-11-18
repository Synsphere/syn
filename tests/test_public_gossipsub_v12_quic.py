"""Mixed public GossipSub v1.1/v1.2 and isolated QUIC config gates."""
from types import SimpleNamespace

import pytest
import trio
from libp2p.pubsub.gossipsub import GossipSub, PROTOCOL_ID_V11, PROTOCOL_ID_V12
from libp2p.pubsub.pb import rpc_pb2

from syn.config import P2PConfig, loadConfig
from syn.p2p import requested_listen_addresses
from syn.public_pubsub import PublicPubsubService, MAX_IDONTWANT_IDS
from test_mesh_pubsub import secure_host


def isolated(**kwargs):
    return P2PConfig(mdnsEnabled=False, upnpEnabled=False, bootstrapPeers=(),
                     allowPrivateAddresses=True, **kwargs)


def test_default_v12_with_v11_fallback_and_configurable_v11(tmp_path):
    modern = PublicPubsubService(secure_host(), None, isolated(), lambda: ())
    assert modern.router.protocols == [PROTOCOL_ID_V12, PROTOCOL_ID_V11]
    assert modern.status()["gossipsub_version"] == "1.2"
    legacy = PublicPubsubService(secure_host(), None,
                isolated(publicGossipsubVersion="1.1"), lambda: ())
    assert legacy.router.protocols == [PROTOCOL_ID_V11]
    config = tmp_path / "syn.toml"
    config.write_text('[database]\nurl = "sqlite://"\n[p2p]\n'
                      'publicGossipsubVersion = "1.1"\nquicEnabled = true\n'
                      'quicListenAddresses = ["/ip4/127.0.0.1/udp/7777/quic-v1"]\n')
    parsed = loadConfig(config).p2p
    assert parsed.publicGossipsubVersion == "1.1" and parsed.quicEnabled
    assert parsed.quicListenAddresses == ("/ip4/127.0.0.1/udp/7777/quic-v1",)


@pytest.mark.parametrize("key,value", [
    ("publicGossipsubVersion", "2.0"),
    ("publicGossipsubVersion", None),
    ("quicEnabled", 1),
    ("quicListenAddresses", ()),
    ("quicListenAddresses", ("/ip4/0.0.0.0/tcp/4001",)),
    ("quicListenAddresses", ("/ip4/0.0.0.0/udp/abc/quic-v1",)),
    ("quicListenAddresses", ("/ip6/::1/udp/1/quic-v2",)),
    ("quicListenAddresses", ("/ip4/0.0.0.0/udp/1/quic-v1",)*2),
])
def test_invalid_transport_or_protocol_config_rejected(key, value):
    if key == "quicListenAddresses" and value == ():
        kwargs = {"quicEnabled": True, key: value}
    else:
        kwargs = {key: value}
    with pytest.raises(ValueError, match="p2p"):
        isolated(**kwargs)


def test_enabled_quic_adds_udp_without_removing_existing_tcp():
    basic = requested_listen_addresses(isolated(quicEnabled=False,
        webRtcEnabled=False, webTransportEnabled=False))
    assert all("/udp/" not in str(addr) for addr in basic)
    hybrid = requested_listen_addresses(isolated(
        listenAddresses=("/ip4/0.0.0.0/tcp/4001",),
        quicEnabled=True, webRtcEnabled=False, webTransportEnabled=False,
        quicListenAddresses=("/ip4/0.0.0.0/udp/4001/quic-v1",)))
    assert [str(addr) for addr in hybrid] == [
        "/ip4/0.0.0.0/tcp/4001", "/ip4/0.0.0.0/udp/4001/quic-v1"]


@pytest.mark.parametrize("version,expected", [(PROTOCOL_ID_V12, 1),
                                               (PROTOCOL_ID_V11, 0)])
def test_idontwant_control_only_from_negotiated_v12_peers(monkeypatch, version, expected):
    captured = []
    async def capture(self, packet, sender):
        snapshot = rpc_pb2.RPC()
        snapshot.CopyFrom(packet)
        captured.append(snapshot)
    monkeypatch.setattr(GossipSub, "handle_rpc", capture)
    service = PublicPubsubService(secure_host(), None, isolated(), lambda: ())
    peer = "synthetic-peer"
    service.pubsub.peers[peer] = SimpleNamespace()
    service.router.peer_protocol[peer] = version
    packet = rpc_pb2.RPC()
    packet.control.idontwant.add().messageIDs.extend([b"good-id", b"", b"x" * 150])
    packet.control.prune.add(topicID="/off-topic")
    trio.run(service.router.handle_rpc, packet, peer)
    assert len(captured) == 1
    assert len(captured[0].control.idontwant) == expected
    if expected:
        assert list(captured[0].control.idontwant[0].messageIDs) == [b"good-id"]
    assert not captured[0].control.prune


def test_real_quic_and_tcp_host_sockets_can_coexist():
    # A local-only transport qualification: public UDP forwarding is separate.
    from libp2p import new_host
    from libp2p.crypto.ed25519 import create_new_key_pair
    from libp2p.peer.peerinfo import info_from_p2p_addr
    from multiaddr import Multiaddr

    async def exercise():
        options = isolated(quicEnabled=True, webRtcEnabled=False, webTransportEnabled=False,
            listenAddresses=("/ip4/127.0.0.1/tcp/0",),
            quicListenAddresses=("/ip4/127.0.0.1/udp/0/quic-v1",))
        binds = requested_listen_addresses(options)
        server = new_host(key_pair=create_new_key_pair(), bootstrap=[],
            enable_mDNS=False, enable_upnp=False, listen_addrs=binds, enable_quic=True)
        client = new_host(key_pair=create_new_key_pair(), bootstrap=[],
            enable_mDNS=False, enable_upnp=False, listen_addrs=binds, enable_quic=True)
        with trio.fail_after(15):
            async with server.run(listen_addrs=binds), client.run(listen_addrs=binds):
                current = [str(x) for x in server.get_transport_addrs()]
                assert any("/tcp/" in x for x in current)
                quic = next(x for x in current if "/udp/" in x and "/quic-v1" in x)
                target = info_from_p2p_addr(Multiaddr(quic + "/p2p/" + str(server.get_id())))
                await client.connect(target)
                assert client.get_network().get_connections(server.get_id())
    trio.run(exercise)


def test_idontwant_budget_is_bounded(monkeypatch):
    captured = []
    async def capture(self, packet, sender):
        captured.append(packet)
    monkeypatch.setattr(GossipSub, "handle_rpc", capture)
    service = PublicPubsubService(secure_host(), None, isolated(), lambda: ())
    peer = "synthetic-peer"
    service.pubsub.peers[peer] = object()
    service.router.peer_protocol[peer] = PROTOCOL_ID_V12
    packet = rpc_pb2.RPC()
    packet.control.idontwant.add().messageIDs.extend([bytes([i])*32 for i in range(30)])
    trio.run(service.router.handle_rpc, packet, peer)
    assert sum(len(item.messageIDs) for item in captured[0].control.idontwant) <= MAX_IDONTWANT_IDS
