"""Explicit local signed-governance import; no key handling or implicit migration."""

import argparse
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from syn_client.governance import GovernedPolicyRecord

from syn.config import loadConfig
from syn.governance import GovernedPolicyStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "config",
        "record",
        "workspace",
        "catalog",
        "genesis-digest",
        "predecessor-digest",
    ):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)
    engine = None
    try:
        with Path(args.record).open("rb") as handle:
            raw = handle.read(2_097_153)
        record = GovernedPolicyRecord.from_json(raw.decode("utf-8"))
        # Full proof verification and current admission time are enforced inside
        # the store lock. The CLI supplies no caller-selected clock override.
        engine = create_engine(loadConfig(args.config).database.url, echo=False)
        result = GovernedPolicyStore(sessionmaker(engine)).admit(
            record,
            expected_workspace=args.workspace,
            expected_catalog=args.catalog,
            expected_genesis_digest=args.genesis_digest,
            expected_predecessor_digest=args.predecessor_digest,
        )
        print("Committed signed governance record: " + result.digest)
        return 0
    except (ValueError, OSError, SQLAlchemyError):
        print("Signed governance held: check independent pins, approvals, schema and local access")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
