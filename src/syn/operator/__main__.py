"""Explicit canonical operator initialization, preflight and foreground service."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "legacy-state":
        from .legacy_state import main as legacy_main

        return legacy_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("legacy-state", help="Archive and inspect retired receipt and replay journals")
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
    for name in ("adoption-init", "agent-pulse", "agent-reserve", "agent-grant", "agent-seal", "agent-confirm", "agent-group"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
        if name not in {"adoption-init", "agent-pulse"}:
            sub.add_argument("--request", type=Path, required=True)
    for name in ("lan-advertise", "lan-validate", "lan-intake", "lan-serve"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--host", required=True)
        sub.add_argument("--port", type=int, required=True)
        if name == "lan-serve":
            sub.add_argument("--subnet", required=True)
        elif name != "lan-advertise":
            sub.add_argument("--request", type=Path, required=True)
    for name in ("lan-inbox-init", "lan-pending", "lan-pending-read"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
        if name == "lan-pending-read":
            sub.add_argument("--offer-sha256", required=True)
    for name in ("lan-reserve", "lan-grant"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--offer-sha256", required=True)
        if name == "lan-reserve":
            sub.add_argument("--operator-byte", type=int, required=True)
        else:
            sub.add_argument("--request", type=Path, required=True)
    backup = commands.add_parser("backup-recovery", help="Create an encrypted operator checkpoint with a private recovery phrase")
    backup.add_argument("--config", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    recovery = commands.add_parser("recover-operator", help="Restore one pinned checkpoint into a fresh native store")
    recovery.add_argument("--state-dir", type=Path, required=True)
    recovery.add_argument("--keyring-service", required=True)
    recovery.add_argument("--keyring-account", required=True)
    recovery.add_argument("--backup", type=Path, required=True)
    recovery.add_argument("--expected-base-public-key-hash", required=True)
    recovery.add_argument("--expected-backup-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "unlock":
            from .unlock import unlock
            print(json.dumps(unlock(), sort_keys=True, separators=(",", ":")))
            return 0
        from .service import OperatorServer, initialize, run
        if args.operation.startswith("agent-") or args.operation == "adoption-init":
            from .adoption_cli import operate
            from .custody import private_read
            from syn_client.operator_protocol import parse
            payload = parse(private_read(args.request)) if hasattr(args, "request") else None
            value = operate(args.config, args.operation, payload)
        elif args.operation in ("lan-reserve", "lan-grant"):
            from .adoption_cli import approve_selection
            from .custody import private_read
            from .enrollment import terminal_approval
            from .lan_inbox import grant, reserve as reserve_pending
            from syn_client.operator_protocol import canonical, parse

            if args.operation == "lan-reserve":
                value = reserve_pending(
                    args.config, offer=args.offer_sha256, operator_byte=args.operator_byte,
                    approve=terminal_approval,
                )
            else:
                value = grant(
                    args.config, parse(private_read(args.request)), offer=args.offer_sha256,
                    approve=approve_selection,
                )
            sys.stdout.buffer.write(canonical(value))
            return 0
        elif args.operation == "lan-serve":
            from .lan_transport import serve
            try:
                serve(args.config, host=args.host, port=args.port, subnet=args.subnet)
            except KeyboardInterrupt:
                pass
            return 0
        elif args.operation == "lan-advertise":
            from syn.agent.lan_enrollment import advertise
            from syn_client.operator_protocol import canonical
            value = advertise(args.config, host=args.host, port=args.port)
            sys.stdout.buffer.write(canonical(value))
            return 0
        elif args.operation in ("lan-validate", "lan-intake"):
            from syn.agent.lan_enrollment import validate_submission
            from .custody import private_read
            from .lan_inbox import intake
            from syn_client.operator_protocol import parse
            check = validate_submission if args.operation == "lan-validate" else intake
            value = check(args.config, parse(private_read(args.request)), host=args.host, port=args.port)
        elif args.operation == "lan-inbox-init":
            from .lan_inbox import initialize as initialize_inbox
            value = initialize_inbox(args.config)
        elif args.operation in ("lan-pending", "lan-pending-read"):
            from .lan_inbox import pending
            from syn_client.operator_protocol import canonical
            value = pending(args.config, offer=getattr(args, "offer_sha256", None))
            if args.operation == "lan-pending-read":
                sys.stdout.buffer.write(canonical(value))
                return 0
        elif args.operation == "init":
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
        elif args.operation == "backup-recovery":
            from .recovery import backup_operator

            value = backup_operator(args.config, args.output)
        elif args.operation == "recover-operator":
            from .recovery import recover_operator

            value = recover_operator(
                args.state_dir, service=args.keyring_service, account=args.keyring_account,
                backup=args.backup, expected_base_hash=args.expected_base_public_key_hash,
                expected_backup_sha256=args.expected_backup_sha256,
            )
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
