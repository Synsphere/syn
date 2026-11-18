"""Public Syn URL groups, bounded wire messages and a local HTTP client.

These groups are open network namespaces. A signed transport publication does
not establish ownership of its URL or authorize execution, storage or consensus.
This module imports neither the Syn runtime nor libp2p.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import secrets
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .protocol import SynError, canonical


GROUP_PROFILE = "syn.public-gossipsub-group/1"
MESSAGE_PROFILE = "syn.public-gossipsub-message/1"
MAX_MESSAGE_BYTES = 65_536
MAX_JSON_DEPTH = 64
# A maximum page contains 256 bounded messages plus receive metadata.
MAX_HTTP_RESPONSE_BYTES = MAX_MESSAGE_BYTES * 256 + 1_048_576
# A received wire envelope sits inside the page's messages array.
MAX_HTTP_JSON_DEPTH = MAX_JSON_DEPTH + 2
LOCAL_KINDS = frozenset({"type", "thing", "event", "ember", "cog", "credential",
                         "enigma", "pondle", "snug", "om"})
_URL = re.compile(r"(?:syn|synny)://(" + "|".join(sorted(LOCAL_KINDS))
                  + r")/([a-z0-9]+(?:[-_][a-z0-9]+)*)\Z")
_NETWORK = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MESSAGE_ID = re.compile(r"[0-9a-f]{32}\Z")
_MESSAGE_FIELDS = frozenset({"profile", "network_id", "url", "message_id", "sent_at", "data"})


class PublicPubsubError(ValueError):
    """Invalid public group or message, without exposing its contents."""


class PublicMessageTooLarge(PublicPubsubError):
    """The canonical public message exceeds its byte bound."""


def canonical_url(value: str) -> str:
    """Normalize the supported scheme alias; require an otherwise exact URL."""
    if type(value) is not str or not 1 <= len(value) <= 280:
        raise PublicPubsubError("Expected a bounded canonical Syn object URL")
    match = _URL.fullmatch(value)
    if match is None or len(match[2]) > 255:
        raise PublicPubsubError("Expected a canonical Syn object URL")
    return "syn://" + match[1] + "/" + match[2]


def _network(value: str) -> str:
    if type(value) is not str or _NETWORK.fullmatch(value) is None:
        raise PublicPubsubError("Expected a lowercase Syn network identifier")
    return value


def group_descriptor(network_id: str, url: str) -> dict[str, str]:
    """Stable group identity shared across Operators, sessions and transports."""
    body = {"profile": GROUP_PROFILE, "network_id": _network(network_id), "url": canonical_url(url)}
    digest = hashlib.sha256(canonical(body)).digest()
    return {**body, "group_hash": digest.hex(),
            "topic": "/syn/public-pubsub/1/" + digest.hex(),
            "provider_key": (b"\x12\x20" + digest).hex()}


def group_hash(network_id: str, url: str) -> str:
    return group_descriptor(network_id, url)["group_hash"]


def provider_key(network_id: str, url: str) -> bytes:
    return bytes.fromhex(group_descriptor(network_id, url)["provider_key"])


def topic(network_id: str, url: str) -> str:
    return group_descriptor(network_id, url)["topic"]


def _check_text(value: str) -> None:
    # Escaped unpaired surrogates pass Python's JSON parser but cannot cross
    # UTF-8 HTTP responses. Reject them before retaining or publishing data.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise PublicPubsubError("Public pub-sub JSON strings must be valid Unicode") from None


def _check_json(value: Any, *, max_bytes: int = MAX_MESSAGE_BYTES,
                max_depth: int = MAX_JSON_DEPTH) -> None:
    """Bound nesting and a conservative byte minimum before serializing.

    Depth counts from the entire document at zero. Only JSON types are allowed;
    Python tuples, non-string keys, non-finite floats and cyclic structures are
    not alternate encodings. Exact encoded size is checked after serialization.
    """
    pending, minimum = [(value, 0)], 0
    while pending:
        current, depth = pending.pop()
        if depth > max_depth:
            raise PublicPubsubError("Public pub-sub JSON exceeds its nesting limit")
        kind = type(current)
        if kind is dict:
            minimum += 2 + max(0, len(current) - 1) + len(current)
            if minimum > max_bytes:
                raise PublicMessageTooLarge("Public pub-sub message exceeds its byte limit")
            for key, item in current.items():
                if type(key) is not str:
                    raise PublicPubsubError("Public pub-sub JSON object keys must be strings")
                minimum += 2 + len(key)
                if minimum > max_bytes:
                    raise PublicMessageTooLarge("Public pub-sub message exceeds its byte limit")
                _check_text(key)
                pending.append((item, depth + 1))
        elif kind is list:
            minimum += 2 + max(0, len(current) - 1)
            if minimum <= max_bytes:
                pending.extend((item, depth + 1) for item in current)
        elif kind is str:
            minimum += 2 + len(current)
            if minimum <= max_bytes:
                _check_text(current)
        elif current is None:
            minimum += 4
        elif kind is bool:
            minimum += 4 if current else 5
        elif kind is int:
            minimum += 1
        elif kind is float and math.isfinite(current):
            minimum += 1
        else:
            raise PublicPubsubError("Public pub-sub requires finite JSON values")
        if minimum > max_bytes:
            raise PublicMessageTooLarge("Public pub-sub message exceeds its byte limit")


def _json_bytes(value: Any) -> bytes:
    _check_json(value)
    try:
        raw = canonical(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise PublicPubsubError("Public pub-sub message cannot be encoded as JSON") from None
    if len(raw) > MAX_MESSAGE_BYTES:
        raise PublicMessageTooLarge("Public pub-sub message exceeds its byte limit")
    return raw


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PublicPubsubError("Public pub-sub JSON contains duplicate fields")
        value[key] = item
    return value


def _invalid_constant(value):
    raise PublicPubsubError("Public pub-sub requires finite JSON values")


def _message_identity(value, sent_at):
    if type(value) is not str or _MESSAGE_ID.fullmatch(value) is None:
        raise PublicPubsubError("Expected a 128-bit lowercase public message identifier")
    if type(sent_at) is not int or sent_at < 0:
        raise PublicPubsubError("Expected an integer Unix publication time")


def encode_message(network_id: str, url: str, data: Any, *,
                   message_id: str | None = None, sent_at: int | None = None) -> bytes:
    """Encode an exact bounded public message; receiving checks its freshness."""
    identity = secrets.token_hex(16) if message_id is None else message_id
    timestamp = int(time.time()) if sent_at is None else sent_at
    _message_identity(identity, timestamp)
    return _json_bytes({"profile": MESSAGE_PROFILE, "network_id": _network(network_id),
                        "url": canonical_url(url), "message_id": identity,
                        "sent_at": timestamp, "data": data})


def decode_message(raw: bytes, *, network_id: str, url: str, now=None) -> dict[str, Any]:
    """Validate the exact expected namespace and a five-minute receive window."""
    expected_network, expected_url = _network(network_id), canonical_url(url)
    if type(raw) is not bytes or not raw:
        raise PublicPubsubError("Expected a nonempty public pub-sub byte message")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise PublicMessageTooLarge("Public pub-sub message exceeds its byte limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise PublicPubsubError("Invalid public pub-sub JSON message") from None
    if type(value) is not dict or set(value) != _MESSAGE_FIELDS:
        raise PublicPubsubError("Public pub-sub message fields differ from the wire profile")
    if _json_bytes(value) != raw:
        raise PublicPubsubError("Public pub-sub message is not canonical JSON")
    if (value["profile"] != MESSAGE_PROFILE or value["network_id"] != expected_network
            or value["url"] != expected_url):
        raise PublicPubsubError("Public pub-sub message belongs to another group")
    _message_identity(value["message_id"], value["sent_at"])
    current = int(time.time()) if now is None else now
    if (type(current) not in (int, float) or current < 0
            or (type(current) is float and not math.isfinite(current))):
        raise PublicPubsubError("Invalid public pub-sub verification time")
    if value["sent_at"] < current - 300 or value["sent_at"] > current + 30:
        raise PublicPubsubError("Public pub-sub message is stale or from the future")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _hash_text(value: str) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise PublicPubsubError("Expected a lowercase public group hash")
    return value


class PublicPubsubClient:
    """Synchronous local control client; no workspace or private-mesh credential.

    The server is a trusted local control plane. HTTPS is required beyond
    loopback. Publications are never retried: a lost HTTP reply does not prove
    that the first publication was absent.
    """

    def __init__(self, url: str, *, timeout: float = 10.0):
        if (type(url) is not str or not 1 <= len(url) <= 4096
                or any(character.isspace() or ord(character) < 32 for character in url)
                or "?" in url or "#" in url):
            raise PublicPubsubError("Expected a valid public pub-sub service URL")
        try:
            parsed = urlsplit(url)
            port = parsed.port
            try:
                loopback = (parsed.hostname == "localhost"
                            or ipaddress.ip_address(parsed.hostname or "").is_loopback)
            except ValueError:
                loopback = False
            if ((parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
                    or not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or parsed.path not in {"", "/"} or (port is not None and not 1 <= port <= 65535)):
                raise ValueError
        except ValueError:
            raise PublicPubsubError("Service URL requires HTTPS or loopback HTTP without a path or credentials") from None
        if (type(timeout) not in (int, float) or not 0 < timeout <= 120
                or (type(timeout) is float and not math.isfinite(timeout))):
            raise PublicPubsubError("Public pub-sub timeout must be finite and within 120 seconds")
        self.url, self.timeout = url.rstrip("/"), timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, method: str, suffix: str = "", body=None) -> dict[str, Any]:
        request = Request(self.url + "/p2p/pubsub/groups" + suffix,
                          method=method, data=None if body is None else _json_bytes(body),
                          headers={"Accept": "application/json", "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
                if len(raw) > MAX_HTTP_RESPONSE_BYTES:
                    raise SynError("Public pub-sub response exceeded its byte limit")
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                   parse_constant=_invalid_constant)
                if type(value) is not dict:
                    raise SynError("Public pub-sub returned an invalid response")
                _check_json(value, max_bytes=MAX_HTTP_RESPONSE_BYTES,
                            max_depth=MAX_HTTP_JSON_DEPTH)
                return value
        except HTTPError as error:
            code = error.code
            error.close()
            raise SynError(f"Public pub-sub request failed (HTTP {code})") from None
        except (URLError, OSError, ValueError, UnicodeError, RecursionError, OverflowError):
            raise SynError("Public pub-sub request unavailable or invalid") from None

    def groups(self) -> dict[str, Any]:
        return self._request("GET")

    def join(self, url: str, network_id: str | None = None) -> dict[str, Any]:
        body = {"url": canonical_url(url)}
        if network_id is not None:
            body["network_id"] = _network(network_id)
        return self._request("POST", body=body)

    def leave(self, group_hash: str) -> dict[str, Any]:
        return self._request("DELETE", "/" + _hash_text(group_hash))

    def publish(self, group_hash: str, data: Any) -> dict[str, Any]:
        return self._request("POST", "/" + _hash_text(group_hash) + "/messages", {"data": data})

    def messages(self, group_hash: str, after: str | None = None, limit: int = 100) -> dict[str, Any]:
        identity = _hash_text(group_hash)
        if type(limit) is not int or not 1 <= limit <= 256:
            raise PublicPubsubError("Public pub-sub receive limit must be between 1 and 256")
        query = {"limit": limit}
        if after is not None:
            if type(after) is not str or not 1 <= len(after) <= 512:
                raise PublicPubsubError("Expected a bounded opaque public pub-sub cursor")
            query["after"] = after
        return self._request("GET", "/" + identity + "/messages?" + urlencode(query))
