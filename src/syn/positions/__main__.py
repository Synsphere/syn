"""Trusted local inspection; no unauthenticated location endpoint is installed."""
from __future__ import annotations
import argparse
import json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--node-ref", help="Existing node reference; does not create an identity")
    parser.add_argument("operation", choices=("current", "verify", "providers", "bootstrap"))
    args = parser.parse_args(argv)
    from syn.config import loadConfig
    from syn.database import createEngine, createSessionFactory
    from .runtime import PositionRuntime
    from .store import PositionStore
    from .schema import nodes
    from sqlalchemy import select
    config = loadConfig(args.config)
    engine = createEngine(config.database.url, echo=False, migrate=config.database.autoMigrate)
    try:
        sessions = createSessionFactory(engine)
        store = PositionStore(sessions)
        nodeRef = args.node_ref or config.position.nodeRef
        if nodeRef is None:
            runtime = PositionRuntime(sessions, config.database.url, config.position)
            nodeRef = runtime.nodeRef
        if args.operation == "current":
            value = store.current(nodeRef)
        elif args.operation == "verify":
            value = store.verify(nodeRef)
        elif args.operation == "providers":
            with sessions() as session:
                value = session.execute(select(nodes.c.probeReport).where(nodes.c.nodeRef == nodeRef)).scalar_one_or_none()
        else:
            from .bootstrap import bootstrap
            value = store.bootstrap(nodeRef, bootstrap(config.position))
        print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
