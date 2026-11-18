#!/usr/bin/env python3
"""Independently verify a public Syn peer on a specific UDP transport.

One pinned PeerID, one transport, fresh challenge. Run this command from a
different network to test the actual inbound route; it never changes routers,
firewalls, host custody, advertisements or Syn's database.
"""
from __future__ import annotations

import argparse
import json
import sys


def _error(name: str) -> int:
    # Avoid printing untrusted hostname, network error strings or Peer records.
    print(json.dumps({"verified": False, "reason": name,
                      "scope": "witness-vantage-only"}, sort_keys=True), file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("peer", help="Pinned /ip4/.../udp/.../<transport>/p2p/<PeerID> or /dns4/... form")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--allow-private", action="store_true",
                        help="Isolated laboratory only; may dial private/loopback network addresses")
    args = parser.parse_args(argv)

    try:
        from libp2p import new_host
        from libp2p.crypto.ed25519 import create_new_key_pair
        from libp2p.peer.peerinfo import info_from_p2p_addr
        from multiaddr import Multiaddr
        import trio
        import dns.asyncresolver
        from syn.dns4_dial import resolve_dns4
        from syn.p2p_transport_probe import target_kind, verify_transport
    except ImportError:
        return _error("MissingRuntimeDependency")

    try:
        original = Multiaddr(args.peer)
        protocols = [protocol.name for protocol in original.protocols()]
        if len(args.peer) > 640 or protocols[-1:] != ["p2p"]:
            raise ValueError
        if type(args.timeout) not in (int, float) or not 0 < args.timeout <= 30:
            raise ValueError
        peer = info_from_p2p_addr(original)
        if not peer.addrs:
            raise ValueError
    except (TypeError, ValueError, KeyError):
        return _error("InvalidPinnedEndpoint")

    async def run():
        nonlocal peer
        endpoint = str(peer.addrs[0])
        if endpoint.startswith("/dns4/"):
            async def lookup(hostname):
                try:
                    answers = await dns.asyncresolver.resolve(hostname, "A", lifetime=min(3, args.timeout))
                    return [answer.address for answer in answers][:4]
                except Exception:
                    return []
            resolved = await resolve_dns4(endpoint, peer_id=str(peer.peer_id),
                                          resolve_a=lookup, allow_private=args.allow_private)
            if not resolved:
                raise ValueError("DNS4NoPermittedAddresses")
            endpoint = resolved[0]
            from libp2p.peer.peerinfo import PeerInfo
            peer = PeerInfo(peer.peer_id, [Multiaddr(endpoint)])
        kind = target_kind(endpoint)
        # Disallow local routing claims unless the caller explicitly opted into
        # a segregated test swarm. target_kind checks canonical syntax.
        if not args.allow_private:
            import ipaddress
            ip = ipaddress.ip_address(endpoint.split("/")[2])
            if not ip.is_global:
                raise ValueError("PrivateDestinationRefused")
        # No transport fallback: register only the protocol being tested.
        listener = {
            "quic-v1": "/ip4/127.0.0.1/udp/0/quic-v1",
            "webrtc-direct": "/ip4/127.0.0.1/udp/0/webrtc-direct",
            "webtransport": "/ip4/127.0.0.1/udp/0/quic-v1/webtransport",
        }[kind]
        host = new_host(key_pair=create_new_key_pair(), bootstrap=[], listen_addrs=None,
                        enable_mDNS=False, enable_upnp=False, enable_tcp=False,
                        enable_quic=kind == "quic-v1",
                        enable_webrtc=kind == "webrtc-direct",
                        enable_webtransport=kind == "webtransport")
        async with host.run(listen_addrs=[Multiaddr(listener)]):
            return await verify_transport(host, peer, kind=kind, timeout=args.timeout)

    try:
        result = trio.run(run)
    except Exception as error:
        return _error(type(error).__name__)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
