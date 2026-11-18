"""Offline qualification of default adapters and DNS4 security grammar.

A live py-libp2p multi-node test remains mandatory before release.
"""
import asyncio
from dataclasses import dataclass

import pytest

from syn.dns4_dial import resolve_dns4
from syn.transport_capabilities import create_host, enabled_listeners, registered_transports


@dataclass
class Config:
    listenAddresses: tuple = ("/ip4/0.0.0.0/tcp/4001",)
    quicEnabled: bool = True
    quicListenAddresses: tuple = ("/ip4/0.0.0.0/udp/0/quic-v1",)
    webRtcEnabled: bool = True
    webRtcListenAddresses: tuple = ("/ip4/0.0.0.0/udp/0/webrtc-direct",)
    webTransportEnabled: bool = True
    webTransportListenAddresses: tuple = ("/ip4/0.0.0.0/udp/0/quic-v1/webtransport",)
    webSocketEnabled: bool = True
    dns4Enabled: bool = True


def test_default_transport_flags_dont_lose_tcp_when_listeners_are_configured():
    calls = []
    def factory(**kwargs):
        calls.append(kwargs)
        return object()
    config = Config()
    _, status = create_host(factory, object(), config, webrtc_ready=True, bootstrap=[])
    kwargs = calls[0]
    assert kwargs["listen_addrs"] is None
    assert kwargs["enable_tcp"] is True
    assert kwargs["enable_quic"] is True
    assert kwargs["enable_webrtc"] is True
    assert kwargs["enable_webtransport"] is True
    assert kwargs["enable_websocket"] is True
    assert all(value == "enabled" for value in status.values())
    binds = enabled_listeners(config, webrtc_ready=True)
    assert any("/tcp/" in addr for addr in binds)
    assert sum("/udp/" in addr for addr in binds) == 3


def test_missing_webrtc_extra_does_not_disable_other_transports():
    captured = []
    create_host(lambda **kw: captured.append(kw), object(), Config(), webrtc_ready=False)
    assert not captured[0]["enable_webrtc"]
    assert captured[0]["enable_quic"] and captured[0]["enable_webtransport"]
    assert not any("webrtc-direct" in item for item in enabled_listeners(Config(), webrtc_ready=False))


async def resolve(hostname):
    assert hostname == "node.example.org"
    return ["203.0.113.10", "8.8.8.8", "127.0.0.1", "8.8.8.8", "1.1.1.1"]


@pytest.mark.parametrize("suffix", [
    "/tcp/4001",
    "/tcp/443/ws",
    "/udp/4001/quic-v1",
    "/udp/4001/webrtc-direct/certhash/" + "a" * 32,
    "/udp/4001/quic-v1/webtransport/certhash/" + "u" * 32,
])
def test_dns4_resolves_to_concrete_public_ipv4_for_supported_transports(suffix):
    actual = asyncio.run(resolve_dns4("/dns4/node.example.org" + suffix,
         peer_id="pinned", resolve_a=resolve))
    assert actual == ["/ip4/8.8.8.8" + suffix]


@pytest.mark.parametrize("addr", [
    "/dns4/localhost/tcp/4001/p2p/different",
    "/dns4/_srv.example.org/tcp/4001",
    "/dns4/a..example.org/tcp/4001",
    "/dns4/node.example.org/tcp/0",
    "/dns4/node.example.org/tcp/4001/p2p-circuit",
    "/dns4/node.example.org/udp/4001/webrtc-direct",
    "/dns4/node.example.org/udp/4001/quic-v1/webtransport",
])
def test_dns4_rejects_mismatch_and_unsupported_grammar_without_dns(addr):
    async def forbidden(_):
        raise AssertionError("Should not query DNS for bad peer address")
    assert asyncio.run(resolve_dns4(addr, peer_id="pinned", resolve_a=forbidden)) == []


def test_dns4_private_destination_only_with_explicit_isolated_policy():
    async def answer(_): return ["192.168.1.20", "10.0.0.5"]
    addr = "/dns4/node.example.org/tcp/443/p2p/pinned"
    assert asyncio.run(resolve_dns4(addr, peer_id="pinned", resolve_a=answer)) == []
    assert asyncio.run(resolve_dns4(addr, peer_id="pinned", resolve_a=answer,
                                      allow_private=True)) == [
                                          "/ip4/192.168.1.20/tcp/443", "/ip4/10.0.0.5/tcp/443"]


def test_registered_transports_reflect_live_adapters():
    class TCP: pass
    class QUICTransport: pass
    class Manager:
        def get_transports(self): return [TCP(), QUICTransport(), TCP()]
    class Network:
        transport_manager = Manager()
    class Host:
        def get_network(self): return Network()
    assert registered_transports(Host()) == ["QUICTransport", "TCP"]


def test_dns4_deduplicates_and_caps_untrusted_answers():
    async def many(_): return ["1.1.1.1"] * 300
    result = asyncio.run(resolve_dns4(
        "/dns4/node.example.org/tcp/443", peer_id="pin", resolve_a=many))
    assert result == ["/ip4/1.1.1.1/tcp/443"]


def test_dns4_pinned_suffix_stripped_without_altering_transport():
    async def answer(_): return ["8.8.8.8"]
    result = asyncio.run(resolve_dns4(
        "/dns4/node.example.org/udp/4001/quic-v1/p2p/pinned",
        peer_id="pinned", resolve_a=answer))
    assert result == ["/ip4/8.8.8.8/udp/4001/quic-v1"]
