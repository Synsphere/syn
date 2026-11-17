"""Explicit public peer policy. No peer/key is learned from an untrusted reply."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
import re
from urllib.parse import urlsplit

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_NETWORK = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class Peer:
    operator: str
    network: str
    url: str
    allowHttp: bool = False
    initialCheckpoint: str | None = None

    def __post_init__(self):
        if not isinstance(self.operator, str) or not _HASH.fullmatch(self.operator):
            raise ValueError("Witness peer requires an independent lowercase Operator SHA-256 pin")
        if not isinstance(self.network, str) or not _NETWORK.fullmatch(self.network):
            raise ValueError("Invalid witness peer network")
        if type(self.allowHttp) is not bool:
            raise ValueError("allowHttp must be a boolean")
        if self.initialCheckpoint is not None and (
            not isinstance(self.initialCheckpoint, str) or not _HASH.fullmatch(self.initialCheckpoint)
        ):
            raise ValueError("Invalid initial checkpoint digest")
        if (not isinstance(self.url, str) or len(self.url) > 2048
                or not self.url.isascii() or any(ord(c) <= 32 for c in self.url)):
            raise ValueError("Invalid peer URL")
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError as error:
            raise ValueError("Invalid peer endpoint") from error
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or (port is not None and not 1 <= port <= 65535)
                or "\\" in self.url or "%" in parsed.netloc):
            raise ValueError("Peer URL must be an explicit HTTP(S) origin without credentials or extra path")
        if parsed.scheme == "http" and not self.allowHttp:
            raise ValueError("HTTP requires allowHttp=true; prefer certificate-verified HTTPS")
        object.__setattr__(self, "url", self.url.rstrip("/"))

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown witness peer settings")
        try:
            return cls(**value)
        except TypeError as error:
            raise ValueError("Missing or invalid witness peer fields") from error

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WitnessConfig:
    peers: tuple[Peer, ...] = ()
    pollSeconds: float = 120.0
    timeoutSeconds: float = 5.0

    def __post_init__(self):
        if not isinstance(self.peers, (tuple, list)) or len(self.peers) > 32:
            raise ValueError("Configure at most 32 explicitly pinned witness peers")
        peers = tuple(p if isinstance(p, Peer) else Peer.from_dict(p) for p in self.peers)
        if len({p.operator for p in peers}) != len(peers):
            raise ValueError("Duplicate witness Operator pin")
        object.__setattr__(self, "peers", peers)
        for name, minimum, maximum in (("pollSeconds", 15, 86400), ("timeoutSeconds", .1, 30)):
            value = getattr(self, name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not minimum <= value <= maximum):
                raise ValueError(f"Invalid witnessing.{name}")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown witnessing settings")
        return cls(**value)

    def to_dict(self):
        return {"peers": [p.to_dict() for p in self.peers], "pollSeconds": self.pollSeconds,
                "timeoutSeconds": self.timeoutSeconds}
