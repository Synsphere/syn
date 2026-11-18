"""Opt-in public Amino bootstrap smoke test; publishes no provider records.

Run from the repository's installed environment:
    python scripts/check_amino_bootstrap.py

Creates a temporary transport identity and connects to the official IPFS seeds.
Reports DNS resolution separately from authenticated DHT replies. Exit status is
zero when at least one DHT server answered, or one when discovery was unavailable.
"""
from __future__ import annotations

import argparse
import json
import logging
import time

import trio
from multiaddr import Multiaddr

from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.crypto.x25519 import create_new_key_pair as noise_keys
from libp2p.security.noise.transport import Transport as NoiseTransport
from libp2p.stream_muxer.yamux.yamux import Yamux

from syn.amino import AMINO_PROTOCOL, IPFS_BOOTSTRAP_PEERS, AminoDHTClient


async def check(total_timeout: float, request_timeout: float) -> int:
    identity = create_new_key_pair()
    host = new_host(
        key_pair=identity, bootstrap=[], enable_upnp=False,
        sec_opt={"/noise": NoiseTransport(identity, noise_keys().private_key)},
        muxer_opt={"/yamux/1.0.0": Yamux},
    )
    report = {
        "protocol": AMINO_PROTOCOL,
        "security": "Noise",
        "muxer": "Yamux",
        "temporaryPeerId": str(host.get_id()),
        "publicationAttempted": False,
        "dns": [],
        "requestErrors": [],
        "authenticatedDHTResponses": 0,
    }
    responsive = set()

    class DiagnosticClient(AminoDHTClient):
        async def _resolve_bootstrap(self, value):
            try:
                info = await super()._resolve_bootstrap(value)
                report["dns"].append({
                    "seed": value,
                    "addresses": [] if info is None else [str(a) for a in info.addrs],
                })
                return info
            except Exception as exc:
                report["dns"].append({"seed": value, "error": type(exc).__name__,
                                      "detail": str(exc)[:300]})
                raise

        async def _request(self, info, message, **kwargs):
            try:
                response = await super()._request(info, message, **kwargs)
                if response is not None:
                    report["authenticatedDHTResponses"] += 1
                    responsive.add(str(info.peer_id))
                return response
            except Exception as exc:
                if len(report["requestErrors"]) < 12:
                    report["requestErrors"].append({
                        "peerId": str(info.peer_id), "error": type(exc).__name__,
                        "detail": str(exc)[:300],
                    })
                raise

    client = DiagnosticClient(host, IPFS_BOOTSTRAP_PEERS, timeout=request_timeout)
    started = time.monotonic()
    try:
        with trio.move_on_after(total_timeout) as deadline:
            async with host.run(listen_addrs=[Multiaddr("/ip4/0.0.0.0/tcp/0")]):
                await client.bootstrap()
        report["deadlineExpired"] = deadline.cancelled_caught
    except Exception as exc:
        report["exception"] = {"error": type(exc).__name__, "detail": str(exc)[:300]}
    report["elapsedSeconds"] = round(time.monotonic() - started, 3)
    report["responsivePeerIds"] = sorted(responsive)
    report["status"] = client.status()
    print(json.dumps(report, indent=2), flush=True)
    return 0 if responsive else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=55.0, help="Overall seconds (default: 55)")
    parser.add_argument("--request-timeout", type=float, default=4.0, help="RPC/DNS seconds (default: 4)")
    args = parser.parse_args()
    if not 0 < args.timeout <= 120 or not 0 < args.request_timeout <= args.timeout:
        parser.error("Require 0 < request-timeout <= timeout <= 120 seconds")
    logging.getLogger().setLevel(logging.ERROR)
    return trio.run(check, args.timeout, args.request_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
