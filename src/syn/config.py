from __future__ import annotations

import tomllib
import ipaddress
import math
from dataclasses import dataclass
from pathlib import Path

from .positions.config import PositionConfig
from .clocks.config import ClockConfig
from .radios.config import RadioConfig
from .witnessing.config import WitnessConfig
from .cardano.config import CardanoConfig
from .bluetooth.config import BluetoothConfig
from .p2p_defaults import IPFS_BOOTSTRAP_PEERS


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
    upnpMulticastInterface: str | None = None
    upnpDiscoveryDelayMilliseconds: int = 2000
    listenAddresses: tuple[str, ...] = (
        "/ip4/0.0.0.0/tcp/0",
        "/ip6/::/tcp/0",
    )
    bootstrapPeers: tuple[str, ...] = IPFS_BOOTSTRAP_PEERS
    startupTimeoutSeconds: float = 15.0
    aminoEnabled: bool = True
    publishOperatorRecord: bool = True
    announceAddresses: tuple[str, ...] = ()
    # Only for isolated test swarms. Public Amino must use public addresses.
    allowPrivateAddresses: bool = False
    requestTimeoutSeconds: float = 10.0
    lookupTimeoutSeconds: float = 45.0
    bootstrapRefreshSeconds: float = 600.0
    providerRepublishSeconds: float = 22 * 60 * 60
    publicPubsubEnabled: bool = True
    publicPubsubMaxGroups: int = 32
    publicPubsubQueueSize: int = 256
    publicPubsubDiscoveryIntervalSeconds: float = 15.0
    publicPubsubProviderRepublishSeconds: float = 3 * 60 * 60

    def __post_init__(self):
        for name in ("enabled", "mdnsEnabled", "upnpEnabled", "aminoEnabled",
                     "publishOperatorRecord", "allowPrivateAddresses", "publicPubsubEnabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"p2p.{name} must be true or false")
        if self.upnpMulticastInterface is not None:
            try:
                address = ipaddress.ip_address(self.upnpMulticastInterface)
                if (type(self.upnpMulticastInterface) is not str or address.version != 4
                        or str(address) != self.upnpMulticastInterface or address.is_unspecified
                        or address.is_multicast or address.is_loopback or address.is_reserved):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("p2p.upnpMulticastInterface must be a local unicast IPv4 address") from None
        if (type(self.upnpDiscoveryDelayMilliseconds) is not int
                or not 100 <= self.upnpDiscoveryDelayMilliseconds <= 10000):
            raise ValueError("p2p.upnpDiscoveryDelayMilliseconds must be an integer from 100 to 10000")
        for name, limit, empty in (("listenAddresses", 16, False),
                                   ("bootstrapPeers", 32, True),
                                   ("announceAddresses", 16, True)):
            values = getattr(self, name)
            if (not isinstance(values, tuple) or len(values) > limit
                    or (not values and not empty)
                    or any(not isinstance(v, str) or not 0 < len(v) <= 512 for v in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f"p2p.{name} must be a bounded, unique string array")
        for name, lower, upper in (("startupTimeoutSeconds", 0, 300),
                                   ("requestTimeoutSeconds", 0, 60),
                                   ("lookupTimeoutSeconds", 0, 120),
                                   ("bootstrapRefreshSeconds", 9, 86400),
                                   ("providerRepublishSeconds", 59, 22 * 60 * 60)):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not lower < value <= upper):
                raise ValueError(f"p2p.{name} must be finite and between {lower} and {upper}")
        for name, upper in (("publicPubsubMaxGroups", 128), ("publicPubsubQueueSize", 4096)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(f"p2p.{name} must be an integer between 1 and {upper}")
        for name, lower, upper in (("publicPubsubDiscoveryIntervalSeconds", 1, 300),
                                   ("publicPubsubProviderRepublishSeconds", 60, 22 * 60 * 60)):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not lower <= value <= upper):
                raise ValueError(f"p2p.{name} must be finite and between {lower} and {upper}")
        if self.allowPrivateAddresses and any(
                ".bootstrap.libp2p.io/" in address or "/bootstrap.libp2p.io/" in address
                for address in self.bootstrapPeers):
            raise ValueError("p2p.allowPrivateAddresses requires isolated, explicitly selected bootstrapPeers")
        for address in self.announceAddresses:
            parts = address.split("/")
            try:
                if len(parts) != 5 or parts[0] or parts[1] not in {"ip4", "ip6"} or parts[3] != "tcp":
                    raise ValueError
                ip = ipaddress.ip_address(parts[2])
                if (str(ip) != parts[2] or f"ip{ip.version}" != parts[1]
                        or str(int(parts[4])) != parts[4] or not 1 <= int(parts[4]) <= 65535
                        or ip.is_unspecified or ip.is_multicast or ip.is_link_local
                        or (not self.allowPrivateAddresses and not ip.is_global)):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("p2p.announceAddresses requires canonical, dialable IP/TCP addresses") from None


@dataclass(frozen=True, slots=True)
class SynConfig:
    database: DatabaseConfig
    p2p: P2PConfig = P2PConfig()
    position: PositionConfig = PositionConfig()
    clock: ClockConfig = ClockConfig()
    radio: RadioConfig = RadioConfig()
    witnessing: WitnessConfig = WitnessConfig()
    cardano: CardanoConfig = CardanoConfig()
    bluetooth: BluetoothConfig = BluetoothConfig()


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
    bootstrapPeers = p2p.get("bootstrapPeers", list(IPFS_BOOTSTRAP_PEERS))
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
    if type(startupTimeoutSeconds) not in (int, float) or startupTimeoutSeconds <= 0:
        raise ValueError(f"{configPath} p2p.startupTimeoutSeconds must be positive")
    announceAddresses = p2p.get("announceAddresses", [])
    if not isinstance(announceAddresses, list) or not all(isinstance(v, str) for v in announceAddresses):
        raise ValueError(f"{configPath} p2p.announceAddresses must be a string array")

    position = PositionConfig.from_dict(raw.get("position", {}))

    return SynConfig(
        position=position,
        clock=ClockConfig.from_dict(raw.get("clock", {})),
        radio=RadioConfig.from_dict(raw.get("radio", {})),
        witnessing=WitnessConfig.from_dict(raw.get("witnessing", {})),
        cardano=CardanoConfig.from_dict(raw.get("cardano", {})),
        bluetooth=BluetoothConfig.from_dict(raw.get("bluetooth", {})),
        database=DatabaseConfig(url=url, echo=echo, autoMigrate=autoMigrate),
        p2p=P2PConfig(
            enabled=enabled,
            mdnsEnabled=mdnsEnabled,
            upnpEnabled=upnpEnabled,
            upnpMulticastInterface=p2p.get("upnpMulticastInterface"),
            upnpDiscoveryDelayMilliseconds=p2p.get("upnpDiscoveryDelayMilliseconds", 2000),
            listenAddresses=tuple(listenAddresses),
            bootstrapPeers=tuple(bootstrapPeers),
            startupTimeoutSeconds=float(startupTimeoutSeconds),
            aminoEnabled=p2p.get("aminoEnabled", True),
            publishOperatorRecord=p2p.get("publishOperatorRecord", True),
            announceAddresses=tuple(announceAddresses),
            allowPrivateAddresses=p2p.get("allowPrivateAddresses", False),
            requestTimeoutSeconds=p2p.get("requestTimeoutSeconds", 10.0),
            lookupTimeoutSeconds=p2p.get("lookupTimeoutSeconds", 45.0),
            bootstrapRefreshSeconds=p2p.get("bootstrapRefreshSeconds", 600.0),
            providerRepublishSeconds=p2p.get("providerRepublishSeconds", 22 * 60 * 60),
            publicPubsubEnabled=p2p.get("publicPubsubEnabled", True),
            publicPubsubMaxGroups=p2p.get("publicPubsubMaxGroups", 32),
            publicPubsubQueueSize=p2p.get("publicPubsubQueueSize", 256),
            publicPubsubDiscoveryIntervalSeconds=p2p.get("publicPubsubDiscoveryIntervalSeconds", 15.0),
            publicPubsubProviderRepublishSeconds=p2p.get("publicPubsubProviderRepublishSeconds", 3 * 60 * 60),
        ),
    )


def _stringList(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item) for item in value
    )
