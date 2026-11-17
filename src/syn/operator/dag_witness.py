"""Offline Operator DAG proof verifier; does not connect to Syn or a database.

Usage:
  python -m syn.operator.dag_witness checkpoint checkpoint.json --operator HASH
  python -m syn.operator.dag_witness inclusion inclusion.json --operator HASH
  python -m syn.operator.dag_witness consistency consistency.json --operator HASH

Keep your pinned identity and signed checkpoint copies outside the Syn database.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dag_evidence import (verify_checkpoint, verify_inclusion_bundle,
                           verify_consistency_bundle, checkpoint_digest)
from .dag_proofs import _raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("checkpoint", "inclusion", "consistency"))
    parser.add_argument("file", type=Path)
    parser.add_argument("--operator", required=True,
                        help="Independently obtained SHA-256 Operator public-key hash")
    parser.add_argument("--trusted-checkpoint", type=Path,
                        help="Previously saved independent signed checkpoint (consistency only)")
    args = parser.parse_args(argv)
    try:
        _raw(args.operator)
        if args.file.stat().st_size > 131072:
            raise ValueError("Operator proof file exceeds size bound")
        packet = json.loads(args.file.read_text(encoding="utf-8"))
        if args.kind == "checkpoint":
            body = verify_checkpoint(packet)
            if body["operator"] != args.operator:
                raise ValueError("Operator identity differs from independent pin")
            result = {"valid": True, "size": body["sequence"],
                      "checkpoint_digest": checkpoint_digest(packet)}
        else:
            if args.trusted_checkpoint is not None:
                if args.kind != "consistency":
                    raise ValueError("Trusted checkpoint is supported only for consistency proofs")
                if args.trusted_checkpoint.stat().st_size > 131072:
                    raise ValueError("Trusted checkpoint exceeds size limit")
                trusted = json.loads(args.trusted_checkpoint.read_text(encoding="utf-8"))
                old = verify_checkpoint(trusted)
                if old["operator"] != args.operator or packet.get("older") != trusted:
                    raise ValueError("Consistency proof does not extend retained checkpoint")
            check = (verify_inclusion_bundle if args.kind == "inclusion"
                     else verify_consistency_bundle)
            if not check(packet, expected_operator=args.operator):
                raise ValueError("Merkle proof failed")
            result = {"valid": True}
    except Exception as exc:
        # Detailed exception messages may include sensitive file paths.
        print(json.dumps({"valid": False, "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
