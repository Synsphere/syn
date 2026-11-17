"""Inspect the persisted clock state without provisioning or starting a node."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("status", "verify"), nargs="?", default="status")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--node-ref", required=True)
    args = parser.parse_args(argv)
    from syn.config import loadConfig
    from syn.database import createEngine, createSessionFactory
    from .store import ClockStore
    config = loadConfig(args.config)
    engine = createEngine(config.database.url, migrate=False)
    try:
        store = ClockStore(createSessionFactory(engine))
        result = store.verify(args.node_ref) if args.operation == "verify" else store.current(args.node_ref)
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
