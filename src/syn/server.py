from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from .api import createApp
from .config import loadConfig
from .database import createEngine, createSessionFactory
from .feed_jobs import defaultJobHandlers


def serverPort(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parseArguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Syn operator node and API; first run starts operator setup.")
    parser.add_argument(
        "--describe-json", action="store_true",
        help="Print the installed runtime contract without reading config or starting services",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Syn configuration file (default: config.toml)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=serverPort,
        default=8000,
        help="TCP port to bind (default: 8000)",
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default="info",
        dest="logLevel",
        help="Uvicorn log level (default: info)",
    )
    parser.add_argument("--operator-config", type=Path, default=None,
                        help="Existing operator.json, or path for a new operator installation")
    parser.add_argument("--api-only", action="store_true",
                        help="Explicitly run only the API/awareness, without operator setup or service")
    parser.add_argument("--check-config", action="store_true",
                        help="Validate effective configuration without starting services or reading credentials aloud")
    parser.add_argument("--restore-config", action="store_true",
                        help="Explicitly restore the last validated config, preserving the invalid copy")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    options = parseArguments(arguments)
    if options.describe_json:
        from .release import runtimeContract
        print(json.dumps(runtimeContract().to_dict(), sort_keys=True, separators=(",", ":")))
        return
    from .config_lifecycle import (ConfigRecoveryError, configuration_report,
                                   remember_good_config, restore_good_config)
    if options.check_config and options.restore_config:
        raise ValueError("--check-config and --restore-config cannot be combined")
    if options.check_config or options.restore_config:
        if options.restore_config:
            quarantined = restore_good_config(options.config)
            print(f"Restored last-known-good Syn config; damaged copy saved at {quarantined}",
                  file=sys.stderr, flush=True)
        print(json.dumps(configuration_report(options.config), sort_keys=True))
        return
    from .startup import ManagedOperator, ensure_database_config, ensure_operator, handoff_windows
    if handoff_windows(options):
        return
    # Validate before unlocking/creating the Operator; never provision an identity
    # when an invalid TOML file means startup cannot proceed.
    ensure_database_config(options.config)
    config = loadConfig(options.config)
    try:
        remember_good_config(options.config)
    except (OSError, ConfigRecoveryError) as error:
        print(f"Syn config: private recovery snapshot unavailable ({type(error).__name__}); "
              "continuing with validated configuration", file=sys.stderr, flush=True)
    operator = None
    if not options.api_only:
        operator_path = ensure_operator(options.operator_config)
        operator = ManagedOperator(operator_path)
    print("Opening Syn database and preparing node records...", flush=True)
    engine = createEngine(
        config.database.url,
        echo=config.database.echo,
        migrate=config.database.autoMigrate,
    )
    try:
        sessions = createSessionFactory(engine)
        from .positions.runtime import PositionRuntime
        runtime_args = {"clockConfig": config.clock, "radioConfig": config.radio}
        if operator is not None:
            runtime_args["identity"] = operator.server.expected_hash
        runtime = PositionRuntime(sessions, config.database.url, config.position, **runtime_args)
        handlers = {**defaultJobHandlers(config.database.url), **runtime.handlers()}
        app_args = {"positionRuntime": runtime, "cardanoConfig": config.cardano,
                    "bluetoothConfig": config.bluetooth}
        if operator is not None:
            from .operator.dag import run_sync as sync_operator_dag
            handlers["operator-dag-sync"] = partial(sync_operator_dag, config.database.url, operator_path)
            from .witnessing.runtime import HANDLER, handler as witness_handler
            handlers[HANDLER] = witness_handler(config.database.url, operator_path,
                                               runtime.nodeRef, config.witnessing)
            app_args["operatorRuntime"] = operator
            app_args["witnessConfig"] = config.witnessing
        app = createApp(sessions, handlers, config.p2p, **app_args)
        uvicorn.run(
            app,
            host=options.host,
            port=options.port,
            log_level=options.logLevel,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
