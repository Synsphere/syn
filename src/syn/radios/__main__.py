"""Inspect or re-scan the locally configured node's radio capabilities."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("probe", "current", "verify", "record"))
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args(argv)
    from syn.config import loadConfig
    from syn.radios.discovery import RadioDiscovery
    from syn.radios.store import RadioStore
    from syn.database import createEngine, createSessionFactory
    from syn.positions.runtime import PositionRuntime
    if args.command == "probe" and not args.config.is_file():
        from syn.radios.config import RadioConfig
        print(json.dumps([{"provider": p.name, "status": p.status,
                           "errorType": p.errorType,
                           "devices": [d.to_dict() for d in p.devices]}
                          for p in RadioDiscovery(RadioConfig()).scan()], sort_keys=True, indent=2))
        return
    config = loadConfig(args.config)
    if args.command == "probe":
        result = RadioDiscovery(config.radio).scan()
        print(json.dumps([{"provider": p.name, "status": p.status,
                           "errorType": p.errorType,
                           "devices": [d.to_dict() for d in p.devices]} for p in result],
                         sort_keys=True, indent=2))
        return
    engine = createEngine(config.database.url, migrate=config.database.autoMigrate)
    try:
        sessions = createSessionFactory(engine)
        runtime = PositionRuntime(sessions, config.database.url, config.position,
                                  clockConfig=config.clock, radioConfig=config.radio)
        store = RadioStore(sessions)
        if args.command == "record":
            if not config.radio.enabled:
                raise SystemExit("Radio discovery is disabled in the configuration")
            # Existing node setup is still mandatory for signed/hash-chained records.
            from syn.positions.bootstrap import bootstrap
            from syn.positions.store import PositionStore
            position = PositionStore(sessions)
            if not position.current(runtime.nodeRef):
                position.bootstrap(runtime.nodeRef, bootstrap(config.position))
            value = store.record(runtime.nodeRef, RadioDiscovery(config.radio).scan())
        elif args.command == "current":
            value = store.current(runtime.nodeRef)
        else:
            value = store.verify(runtime.nodeRef)
        print(json.dumps(value, sort_keys=True, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
