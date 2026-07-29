from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    url: str
    echo: bool = False
    autoMigrate: bool = True


@dataclass(frozen=True, slots=True)
class SynConfig:
    database: DatabaseConfig


def loadConfig(path: str | Path = "config.toml") -> SynConfig:
    configPath = Path(path)
    with configPath.open("rb") as configFile:
        raw = tomllib.load(configFile)

    try:
        database = raw["database"]
        url = database["url"]
    except (KeyError, TypeError) as error:
        raise ValueError(f"{configPath} must define database.url") from error

    if not isinstance(url, str) or not url:
        raise ValueError(f"{configPath} database.url must be a non-empty string")

    echo = database.get("echo", False)
    if not isinstance(echo, bool):
        raise ValueError(f"{configPath} database.echo must be true or false")

    autoMigrate = database.get("autoMigrate", True)
    if not isinstance(autoMigrate, bool):
        raise ValueError(f"{configPath} database.autoMigrate must be true or false")

    return SynConfig(
        database=DatabaseConfig(url=url, echo=echo, autoMigrate=autoMigrate)
    )
