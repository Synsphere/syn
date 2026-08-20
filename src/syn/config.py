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
class P2PConfig:
    enabled: bool = True
    mdnsEnabled: bool = True
    upnpEnabled: bool = True
    listenAddresses: tuple[str, ...] = (
        "/ip4/0.0.0.0/tcp/0",
        "/ip6/::/tcp/0",
    )
    bootstrapPeers: tuple[str, ...] = ()
    startupTimeoutSeconds: float = 15.0


@dataclass(frozen=True, slots=True)
class SynConfig:
    database: DatabaseConfig
    p2p: P2PConfig = P2PConfig()


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

    p2p = raw.get("p2p", {})
    if not isinstance(p2p, dict):
        raise ValueError(f"{configPath} p2p must be a table")
    enabled = p2p.get("enabled", True)
    mdnsEnabled = p2p.get("mdnsEnabled", True)
    upnpEnabled = p2p.get("upnpEnabled", True)
    listenAddresses = p2p.get(
        "listenAddresses",
        ["/ip4/0.0.0.0/tcp/0", "/ip6/::/tcp/0"],
    )
    bootstrapPeers = p2p.get("bootstrapPeers", [])
    startupTimeoutSeconds = p2p.get("startupTimeoutSeconds", 15.0)
    if not isinstance(enabled, bool):
        raise ValueError(f"{configPath} p2p.enabled must be true or false")
    if not isinstance(mdnsEnabled, bool):
        raise ValueError(f"{configPath} p2p.mdnsEnabled must be true or false")
    if not isinstance(upnpEnabled, bool):
        raise ValueError(f"{configPath} p2p.upnpEnabled must be true or false")
    if not _stringList(listenAddresses):
        raise ValueError(f"{configPath} p2p.listenAddresses must be a non-empty string array")
    if not isinstance(bootstrapPeers, list) or not all(
        isinstance(value, str) and value for value in bootstrapPeers
    ):
        raise ValueError(f"{configPath} p2p.bootstrapPeers must be a string array")
    if not isinstance(startupTimeoutSeconds, (int, float)) or startupTimeoutSeconds <= 0:
        raise ValueError(f"{configPath} p2p.startupTimeoutSeconds must be positive")

    return SynConfig(
        database=DatabaseConfig(url=url, echo=echo, autoMigrate=autoMigrate),
        p2p=P2PConfig(
            enabled=enabled,
            mdnsEnabled=mdnsEnabled,
            upnpEnabled=upnpEnabled,
            listenAddresses=tuple(listenAddresses),
            bootstrapPeers=tuple(bootstrapPeers),
            startupTimeoutSeconds=float(startupTimeoutSeconds),
        ),
    )


def _stringList(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item) for item in value
    )
