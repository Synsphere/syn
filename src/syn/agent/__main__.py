"""Canonical Syn Agent maintenance and foreground process; public output only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    create = commands.add_parser("init")
    create.add_argument("--state-dir", type=Path, required=True)
    create.add_argument("--keyring-service", required=True)
    create.add_argument("--keyring-account", required=True)
    create.add_argument("--expected-operator", required=True)
    create.add_argument("--network-id", required=True)
    create.add_argument("--group-id", required=True)
    create.add_argument("--group-epoch", type=int, required=True)
    create.add_argument("--approved-device-profile", required=True)
    create.add_argument("--new-identity", action="store_true", required=True)
    for operation in ("offer", "lan-offer", "choose", "prepare", "adopt", "adopt-group", "apply-group-rollover", "activate", "serve"):
        sub = commands.add_parser(operation)
        sub.add_argument("--config", type=Path, required=True)
        if operation != "serve":
            sub.add_argument("--request", type=Path, required=True)
        if operation == "lan-offer":
            sub.add_argument("--host", required=True)
            sub.add_argument("--port", type=int, required=True)
    for operation in ("lan-discover", "lan-send"):
        sub = commands.add_parser(operation)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--local-address", required=True)
        sub.add_argument("--subnet", required=True)
        if operation == "lan-discover":
            sub.add_argument("--port", type=int, required=True)
        else:
            sub.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        from syn.operator.custody import private_read
        from syn_client.operator_protocol import exact, parse
        from .device import initialize, load_config
        if args.operation == "init":
            from .session import require_scope
            require_scope()
            value = initialize(args.state_dir, service=args.keyring_service, account=args.keyring_account,
                               expected_operator=args.expected_operator, network_id=args.network_id,
                               group_id=args.group_id, group_epoch=args.group_epoch, approved_profile=args.approved_device_profile)
        elif args.operation == "serve":
            from .service import run
            run(args.config)
            return 0
        elif args.operation in ("lan-discover", "lan-send"):
            from syn_client.operator_protocol import canonical
            from .lan_transport import deliver, discover

            config, _ = load_config(args.config)
            scope = {"local_address": args.local_address, "subnet": args.subnet}
            if args.operation == "lan-discover":
                sys.stdout.buffer.write(canonical(discover(config, port=args.port, **scope)))
                return 0
            value = deliver(config, parse(private_read(args.request)), **scope)
        else:
            config, _ = load_config(args.config)
            payload = parse(private_read(args.request))
            from .enrollment import choose, create_offer, prepare
            if args.operation == "offer":
                value = create_offer(config, args.config.parent, payload)
            elif args.operation == "lan-offer":
                from .lan_enrollment import submit_offer
                from syn_client.operator_protocol import canonical
                value = submit_offer(config, args.config.parent, payload, host=args.host, port=args.port)
                sys.stdout.buffer.write(canonical(value))
                return 0
            elif args.operation == "choose":
                value = choose(config, args.config.parent, payload)
            elif args.operation == "prepare":
                value = prepare(config, args.config.parent, payload, now=int(time.time()))
            elif args.operation == "adopt-group":
                from .group import adopt_group_key
                payload = exact(payload, {"grant", "request", "group"}, "group handoff")
                value = adopt_group_key(config, args.config.parent, grant_packet=payload["grant"],
                    request_packet=payload["request"], envelope=payload["group"], now=int(time.time()))
            elif args.operation == "apply-group-rollover":
                from .group_rollover import apply_rollover
                value = apply_rollover(args.config, payload)
            elif args.operation == "activate":
                from .service import activate
                value = activate(args.config, payload)
            else:
                from .adoption import adopt_selected_key
                payload = exact(payload, {"grant", "request", "envelope"}, "encrypted adoption")
                value = adopt_selected_key(grant_packet=payload["grant"], request_packet=payload["request"],
                    envelope=payload["envelope"], service=config["keyring_service"], account=config["keyring_account"],
                    lock_dir=args.config.parent, expected_master_hash=bytes.fromhex(config["expected_operator"]), now=int(time.time()))
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print("Syn Agent operation refused; check private custody, assignment and host policy", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
