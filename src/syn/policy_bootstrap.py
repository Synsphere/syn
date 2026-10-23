"""Local administrative genesis entry point; never migrates a database implicitly."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from syn_client.policy_adoption import GenesisPolicyAdoption

from syn.config import loadConfig
from syn.policy_adoption import PolicyAdoptionStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["initialize"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--expected-adoption-digest", required=True)
    args = parser.parse_args(argv)
    engine = None
    try:
        # Bound before decoding; never print configuration, document or database URLs.
        with args.manifest.open("rb") as handle:
            raw = handle.read(2_097_153)
        document = GenesisPolicyAdoption.from_json(raw.decode("utf-8"))
        document.resolver(
            expected_workspace_id=args.workspace,
            expected_catalog_id=args.catalog,
            expected_adoption_digest=args.expected_adoption_digest,
        )
        engine = create_engine(loadConfig(args.config).database.url, echo=False)
        committed = PolicyAdoptionStore(sessionmaker(engine)).initialize(
            document,
            expected_workspace_id=args.workspace,
            expected_catalog_id=args.catalog,
            expected_adoption_digest=args.expected_adoption_digest,
        )
        print("Committed local genesis adoption: " + committed.digest)
        return 0
    except (OSError, ValueError, SQLAlchemyError):
        print(
            "Genesis initialization held: check pins, schema, ownership and local database access"
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
