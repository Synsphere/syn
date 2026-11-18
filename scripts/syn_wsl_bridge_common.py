"""Wire and policy primitives for the *opt-in* Windows/WSL network bridge.

No Syn/Operator keys or global host-custody secrets are accepted here. A bridge
has a separate, administrator-provisioned 32-byte secret and an explicit port
policy. Requests are authenticated, freshness-bound, and single-use.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

MAX_CONTROL_BYTES = 4096
CLOCK_SKEW_SECONDS = 30
MAX_NONCES = 2048
_PORT_MIN = 1024
_PORT_MAX = 65535
_SIGNING_CONTEXT = b"syn.wsl.host-bridge.v1\0"
_REPLY_CONTEXT = b"syn.wsl.host-bridge.reply.v1\0"


class BridgeError(ValueError):
    """A validation or ownership fault; message excludes untrusted packet text."""


def canonical(value: object) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                     allow_nan=False).encode("ascii")
    if len(raw) > MAX_CONTROL_BYTES:
        raise BridgeError("Bridge request exceeds its size bound")
    return raw


def secret_file(path: str | os.PathLike[str]) -> bytes:
    """Read an isolated bridge key; on POSIX refuse group/other visibility."""
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise BridgeError("Bridge secret must be a regular, non-symlink file")
    metadata = source.stat()
    if os.name != "nt" and (metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
        raise BridgeError("Bridge secret must have 0600 permissions")
    if metadata.st_size not in (32, 64):
        raise BridgeError("Bridge secret must contain 32 raw bytes or 64 hex characters")
    raw = source.read_bytes()
    if len(raw) == 64:
        try:
            raw = bytes.fromhex(raw.decode("ascii"))
        except (ValueError, UnicodeError):
            raise BridgeError("Malformed bridge secret") from None
    if len(raw) != 32:
        raise BridgeError("Malformed bridge secret")
    return raw


def _mac(secret: bytes, domain: bytes, data: bytes) -> str:
    return hmac.new(secret, domain + data, hashlib.sha256).hexdigest()


def sign_request(secret: bytes, operation: dict, *, now: int | None = None,
                 nonce: str | None = None) -> tuple[bytes, str]:
    now = int(time.time()) if now is None else now
    body = canonical({"version": 1, "timestamp": now,
                      "nonce": nonce or secrets.token_hex(16), "operation": operation})
    return body, _mac(secret, _SIGNING_CONTEXT, body)


def sign_reply(secret: bytes, body: bytes) -> str:
    return _mac(secret, _REPLY_CONTEXT, body)


class ReplayGuard:
    def __init__(self):
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def verify(self, secret: bytes, body: bytes, mac: str, *, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else now
        if len(body) > MAX_CONTROL_BYTES or len(mac) != 64 or not re.fullmatch(r"[0-9a-f]{64}", mac):
            raise BridgeError("Invalid authentication envelope")
        if not hmac.compare_digest(_mac(secret, _SIGNING_CONTEXT, body), mac):
            raise BridgeError("Bridge authentication failed")
        try:
            record = json.loads(body)
        except (UnicodeError, ValueError, RecursionError):
            raise BridgeError("Malformed control request") from None
        if (type(record) is not dict or set(record) != {"version", "timestamp", "nonce", "operation"}
                or record["version"] != 1 or type(record["timestamp"]) is not int
                or abs(now - record["timestamp"]) > CLOCK_SKEW_SECONDS
                or type(record["nonce"]) is not str
                or not re.fullmatch(r"[0-9a-f]{32}", record["nonce"])
                or type(record["operation"]) is not dict or canonical(record) != body):
            raise BridgeError("Invalid, expired or noncanonical bridge request")
        with self._lock:
            for old_nonce, timestamp in list(self._seen.items()):
                if timestamp < now - CLOCK_SKEW_SECONDS:
                    self._seen.pop(old_nonce, None)
            if record["nonce"] in self._seen:
                raise BridgeError("Repeated bridge request")
            self._seen[record["nonce"]] = now
            while len(self._seen) > MAX_NONCES:
                self._seen.popitem(last=False)
        return record["operation"]


def valid_ipv4(value: str, *, private: bool | None = None) -> str:
    if type(value) is not str:
        raise BridgeError("Expected an IPv4 address")
    try:
        ip = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise BridgeError("Expected an IPv4 address") from None
    if (ip.is_unspecified or ip.is_multicast or ip.is_link_local or
            (private is True and not ip.is_private) or
            (private is False and ip.is_private)):
        raise BridgeError("Disallowed bridge IPv4 address")
    return str(ip)


def valid_port(value: int, *, first: int = _PORT_MIN, last: int = _PORT_MAX) -> int:
    if type(value) is not int or not first <= value <= last:
        raise BridgeError("Port is outside the approved bridge range")
    return value


@dataclass(frozen=True)
class BridgePolicy:
    windows_lan: str
    windows_wsl: str
    sandbox_ip: str
    first_port: int = 4001
    last_port: int = 4099
    max_mappings: int = 16
    max_lease_seconds: int = 1800
    allow_tcp: bool = True
    allow_udp: bool = True

    def __post_init__(self):
        for name in ("windows_lan", "windows_wsl", "sandbox_ip"):
            value = valid_ipv4(getattr(self, name))
            object.__setattr__(self, name, value)
        for field in ("first_port", "last_port"):
            valid_port(getattr(self, field))
        if self.first_port > self.last_port or self.last_port - self.first_port > 1023:
            raise BridgeError("Approved bridge range is reversed or too broad")
        if type(self.max_mappings) is not int or not 1 <= self.max_mappings <= 64:
            raise BridgeError("Invalid maximum number of bridge mappings")
        if type(self.max_lease_seconds) is not int or not 60 <= self.max_lease_seconds <= 3600:
            raise BridgeError("Bridge leases must be between 60 and 3600 seconds")
        if type(self.allow_tcp) is not bool or type(self.allow_udp) is not bool:
            raise BridgeError("Bridge protocol permissions must be boolean")
        if self.windows_lan == self.windows_wsl or self.windows_lan == self.sandbox_ip:
            raise BridgeError("Windows LAN must be distinct from WSL NAT endpoints")

    def port(self, port: int) -> int:
        return valid_port(port, first=self.first_port, last=self.last_port)

    def protocol(self, value: str) -> str:
        if value not in ("TCP", "UDP") or type(value) is not str:
            raise BridgeError("Only TCP and UDP port mappings are supported")
        if (value == "TCP" and not self.allow_tcp) or (value == "UDP" and not self.allow_udp):
            raise BridgeError("Protocol is disabled by host policy")
        return value


class BridgeClient:
    """Single-use authenticated control calls; never carries a custody identity."""

    def __init__(self, host: str, port: int, secret: bytes, *, timeout=3.0):
        self.host, self.port, self.secret, self.timeout = valid_ipv4(host), valid_port(port), secret, timeout

    def call(self, operation: dict) -> dict:
        body, mac = sign_request(self.secret, operation)
        request_nonce = json.loads(body)["nonce"]
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("POST", "/v1/control", body=body,
                               headers={"Content-Type": "application/json", "X-Syn-MAC": mac})
            reply = connection.getresponse()
            if int(reply.getheader("Content-Length", "0")) > MAX_CONTROL_BYTES:
                raise BridgeError("Oversized bridge response")
            data = reply.read(MAX_CONTROL_BYTES + 1)
            if len(data) > MAX_CONTROL_BYTES:
                raise BridgeError("Oversized bridge response")
            if not hmac.compare_digest(reply.getheader("X-Syn-MAC", ""), sign_reply(self.secret, data)):
                raise BridgeError("Unauthenticated bridge response")
            packet = json.loads(data)
            if (type(packet) is not dict or set(packet) != {"ok", "value", "requestNonce"}
                    or packet["requestNonce"] != request_nonce or type(packet["ok"]) is not bool):
                raise BridgeError("Malformed or unrelated bridge response")
            if not packet["ok"]:
                raise BridgeError("Host bridge refused mapping")
            return packet["value"]
        except (OSError, ValueError, TimeoutError) as exc:
            if isinstance(exc, BridgeError):
                raise
            raise BridgeError("Windows bridge unavailable or returned invalid data") from None
        finally:
            connection.close()
