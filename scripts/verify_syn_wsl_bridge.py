#!/usr/bin/env python3
"""Read-only, authenticated Windows host bridge reachability test from WSL.

Unlike the transport witness, this checks only the HMAC bridge control plane.
It cannot prove that router mappings, UDP ingress or multicast forwarding work.
No key bytes, host addresses, or router responses are printed.
"""
from __future__ import annotations

import argparse
import json
import sys

from syn_wsl_bridge_common import BridgeClient, BridgeError, secret_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Pinned Windows-host WSL interface IPv4")
    parser.add_argument("--port", type=int, default=48777, help="Bridge control port (default 48777)")
    parser.add_argument("--secret-file", required=True, help="Owner-only existing bridge secret file")
    args = parser.parse_args(argv)
    # The only permitted request here is a fixed read-only action. Do not add
    # 'map', 'delete' or 'query' controls to a health checker.
    try:
        reply = BridgeClient(args.host, args.port, secret_file(args.secret_file), timeout=3).call(
            {"action": "external-ip"})
        if (type(reply) is not dict or reply.get("mappingVerified") is not False
                or reply.get("publicReachability") != "unverified"):
            raise BridgeError("Unexpected host bridge reply")
    except (BridgeError, OSError, TypeError, ValueError):
        print(json.dumps({"schema": "syn.wsl-bridge-health/1", "state": "unavailable",
                          "publicIngress": "not-verified"}, sort_keys=True))
        return 2
    print(json.dumps({"schema": "syn.wsl-bridge-health/1", "state": "authenticated-control-reachable",
                      "publicIngress": "not-verified", "multicastForwarding": "not-probed"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
