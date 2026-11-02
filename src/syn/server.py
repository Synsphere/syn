from __future__ import annotations

import argparse
import json
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
    parser = argparse.ArgumentParser(description="Run the Syn API server.")
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
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    options = parseArguments(arguments)
    if options.describe_json:
        from .release import runtimeContract
        print(json.dumps(runtimeContract().to_dict(), sort_keys=True, separators=(",", ":")))
        return
    config = loadConfig(options.config)
    engine = createEngine(
        config.database.url,
        echo=config.database.echo,
        migrate=config.database.autoMigrate,
    )
    try:
        sessions = createSessionFactory(engine)
        app = createApp(sessions, defaultJobHandlers(config.database.url), config.p2p)
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
