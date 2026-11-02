"""Explicit canonical operator initialization, preflight and foreground service."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("unlock", help="Private local native-keystore prompt; never creates an identity")
    create = commands.add_parser("init", help="Explicitly create one NEW operator identity in the native OS keystore")
    create.add_argument("--state-dir", type=Path, required=True)
    create.add_argument("--keyring-service", required=True)
    create.add_argument("--keyring-account", required=True)
    create.add_argument("--network-id", required=True)
    create.add_argument("--new-identity", action="store_true", required=True)
    for name in ("preflight", "serve"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
    pulse_parser = commands.add_parser("enrollment-pulse", help="Exclusive maintenance: sign a fresh public pulse")
    pulse_parser.add_argument("--config", type=Path, required=True)
    reservation = commands.add_parser("reserve", help="Exclusive maintenance: private human approval of eight public candidates")
    reservation.add_argument("--config", type=Path, required=True)
    reservation.add_argument("--request", type=Path, required=True)
    reservation.add_argument("--operator-byte", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "unlock":
            from .unlock import unlock
            print(json.dumps(unlock(), sort_keys=True, separators=(",", ":")))
            return 0
        from .service import OperatorServer, initialize, run
        if args.operation == "init":
            value = initialize(args.state_dir, service=args.keyring_service, account=args.keyring_account, network_id=args.network_id)
        elif args.operation in ("enrollment-pulse", "reserve"):
            from .enrollment import pulse, reserve, terminal_approval
            if args.operation == "enrollment-pulse":
                value = pulse(args.config)
            else:
                from .custody import private_read
                from syn_client.operator_protocol import exact, parse
                request = exact(parse(private_read(args.request)), {"pulse", "pledge"}, "private enrollment request")
                value = reserve(args.config, request["pulse"], request["pledge"], args.operator_byte, approve=terminal_approval)
        elif args.operation == "preflight":
            server = OperatorServer(args.config)
            current = server.state.read()
            value = {"profile": "syn.operator-preflight/1", "base_public_key_hash": server.expected_hash,
                     "configuration_sha256": server.config_sha, "state_sha256": server.state.digest(current),
                     "agent_activation_authorized": False}
        else:
            run(args.config)
            return 0
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print("Syn operator operation refused; check native keystore, identity and private state", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
