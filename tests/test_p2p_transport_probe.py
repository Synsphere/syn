"""Authenticated transport witness: pure invariants + actual UDP transport tests."""
from __future__ import annotations

import hashlib
import os

import pytest

from syn.p2p_transport_probe import (
    KINDS, NONCE_SIZE, MAX_REQUEST, PROTOCOL,
    make_request, parse_request, response_for, matches_response,
)


@pytest.mark.parametrize("kind", KINDS)
def test_challenge_exact_length_transport_binding_and_freshness(kind):
    first, second = make_request(kind), make_request(kind)
    assert len(first) <= MAX_REQUEST
    assert len(parse_request(first)[0]) == NONCE_SIZE
    assert parse_request(first)[1] == kind
    assert first != second
    peer_id = b"\x12\x20" + hashlib.sha256(b"test remote").digest()
    answer = response_for(first, peer_id, kind)
    assert matches_response(first, peer_id, kind, answer)
    assert not matches_response(second, peer_id, kind, answer)
    other = b"\x12\x20" + hashlib.sha256(b"other").digest()
    assert not matches_response(first, other, kind, answer)


@pytest.mark.parametrize("input", [None, "", "tcp", "webrtc", "quic", 3, True])
def test_unknown_transport_rejected(input):
    with pytest.raises(ValueError):
        make_request(input)


def test_reject_wrong_transport_trailing_bytes_and_truncated_frame():
    peer = b"\x12\x20" + b"x" * 32
    correct = make_request("quic-v1")
    with pytest.raises(ValueError):
        response_for(correct, peer, "webtransport")
    for malformed in (correct[:10], correct[:-1], correct + b"x", correct + b"\x00"):
        with pytest.raises(ValueError):
            parse_request(malformed)


@pytest.mark.parametrize("raw", [b"", b"abc", b"0" * 65, b"x" * 34, "bad"])
def test_peer_id_must_be_binary_and_bounded(raw):
    with pytest.raises(ValueError):
        response_for(make_request("quic-v1"), raw, "quic-v1")


@pytest.mark.parametrize("transport", KINDS)
@pytest.mark.skipif(os.getenv("SYN_TEST_TRANSPORT_LOOPBACK") != "1",
                    reason="Opt-in real libp2p loopback transport acceptance")
def test_real_libp2p_transport_proves_authenticated_challenge(transport):
    """Local network only. This is not a public reachability/Windows IGD test."""
    libp2p = pytest.importorskip("libp2p")
    trio = pytest.importorskip("trio")
    pytest.importorskip("multiaddr")
    if transport == "webrtc-direct":
        for package in ("aiortc", "aioice", "av"):
            pytest.importorskip(package)

    from libp2p.crypto.ed25519 import create_new_key_pair
    from libp2p.peer.peerinfo import info_from_p2p_addr
    from multiaddr import Multiaddr
    from syn.p2p_transport_probe import TransportProbeResponder, verify_transport

    listen = {
        "quic-v1": "/ip4/127.0.0.1/udp/0/quic-v1",
        "webrtc-direct": "/ip4/127.0.0.1/udp/0/webrtc-direct",
        "webtransport": "/ip4/127.0.0.1/udp/0/quic-v1/webtransport",
    }[transport]

    flags = {
        "enable_quic": transport == "quic-v1",
        "enable_webrtc": transport == "webrtc-direct",
        "enable_webtransport": transport == "webtransport",
    }

    async def exercise():
        sender = libp2p.new_host(
            key_pair=create_new_key_pair(), bootstrap=[], listen_addrs=None,
            enable_mDNS=False, enable_upnp=False, enable_tcp=False, **flags)
        receiver = libp2p.new_host(
            key_pair=create_new_key_pair(), bootstrap=[], listen_addrs=None,
            enable_mDNS=False, enable_upnp=False, enable_tcp=False, **flags)
        TransportProbeResponder(receiver).register()
        with trio.fail_after(30):
            async with receiver.run(listen_addrs=[Multiaddr(listen)]):
                async with sender.run(listen_addrs=[Multiaddr(listen)]):
                    # Use the real certhash-bearing transport address, not an
                    # invented one. The remote PeerID is pinned to the endpoint.
                    addresses = receiver.get_transport_addrs()
                    bound = next(
                        addr for addr in addresses
                        if ((transport == "quic-v1" and str(addr).endswith("/quic-v1")) or
                            (transport == "webtransport" and "/webtransport" in str(addr)) or
                            (transport == "webrtc-direct" and "/webrtc-direct" in str(addr)))
                    )
                    base = str(bound)
                    if "/p2p/" not in base:
                        base += "/p2p/" + str(receiver.get_id())
                    endpoint = Multiaddr(base)
                    peer = info_from_p2p_addr(endpoint)
                    proof = await verify_transport(sender, peer, kind=transport, timeout=20)
                    assert proof["verified"] and proof["authenticatedTransport"]
                    assert not proof["operatorIdentityVerified"]
                    assert not proof["publicReachabilityProved"]
                    assert proof["transport"] == transport
    trio.run(exercise)


def test_probe_endpoint_grammar_after_runtime_is_installed():
    pytest.importorskip("multiaddr")
    from syn.p2p_transport_probe import target_kind
    for address, kind in (
        ("/ip4/127.0.0.1/udp/4001/quic-v1", "quic-v1"),
        ("/ip6/::1/udp/4001/quic-v1", "quic-v1"),
    ):
        assert target_kind(address) == kind
    for address in (
        "/ip4/0.0.0.0/udp/0/quic-v1",
        "/ip4/127.0.0.1/tcp/4001",
        "/ip4/127.0.0.1/udp/4001/quic-v1/p2p/QmSomeone",
        "/dns4/peer.example.com/udp/4001/quic-v1",
        "/ip4/224.0.0.251/udp/4001/quic-v1",
    ):
        with pytest.raises(ValueError):
            target_kind(address)
