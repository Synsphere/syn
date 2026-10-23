"""Small HTTP boundary shared by Syn consumers; no database or P2P imports."""

from __future__ import annotations

import ipaddress
import json
import math
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .protocol import (
    GENESIS_HASH,
    MAX_RESPONSE_BYTES,
    IntegrityError,
    SynError,
    canonical,
    uuid_text,
    validate_message,
    verify_receipt,
)

__all__ = ["SynClient", "SynError", "IntegrityError", "GENESIS_HASH"]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SynClient:
    def __init__(self, url: str, token: str, *, timeout: float = 10.0) -> None:
        parsed = urlsplit(url)
        try:
            loopback = (
                parsed.hostname == "localhost"
                or ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            loopback = False
        if (
            (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "Syn URL requires HTTPS, or loopback HTTP, without credentials or a path"
            )
        if (
            not isinstance(token, str)
            or not 32 <= len(token) <= 4096
            or any(not 33 <= ord(c) <= 126 for c in token)
        ):
            raise ValueError("Syn token must be 32..4096 printable characters")
        if (
            isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
        ):
            raise ValueError("Syn timeout must be finite and within 120 seconds")
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout
        # Never forward workspace credentials to redirects or ambient proxies.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(
        self, workspace_id: str, suffix: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        identity = uuid_text(workspace_id)
        request = Request(
            f"{self.url}/v1/communication/workspaces/{identity}/{suffix}",
            data=None if body is None else canonical(body),
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise SynError("Syn response exceeded the configured bound")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise SynError("Syn returned an invalid response")
                return value
        except HTTPError as error:
            code = error.code
            error.close()
            raise SynError(
                f"Syn request failed (HTTP {code}); retry with the same message ID"
            ) from None
        except (URLError, OSError, ValueError):
            raise SynError(
                "Syn request unavailable or invalid; retry with the same message ID"
            ) from None

    def send(
        self,
        workspace_id: str,
        *,
        message_id: str,
        channel: str,
        sender: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        message = validate_message(
            {
                "message_id": message_id,
                "channel": channel,
                "sender": sender,
                "payload": payload,
            }
        )
        receipt = self._request(workspace_id, "messages", message)
        verify_receipt(receipt, workspace_id)
        if canonical({key: receipt[key] for key in message}) != canonical(message):
            raise IntegrityError("Syn acknowledged different message contents")
        return receipt

    def messages(
        self, workspace_id: str, *, after: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        if (
            type(after) is not int
            or not 0 <= after <= 9_007_199_254_740_991
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid Syn page bounds")
        page = self._request(
            workspace_id, "messages?" + urlencode({"after": after, "limit": limit})
        )
        try:
            if (
                set(page) != {"items", "next_cursor", "has_more"}
                or not isinstance(page["items"], list)
                or len(page["items"]) > limit
                or type(page["has_more"]) is not bool
            ):
                raise ValueError("Invalid page")
            cursor = after
            previous = None
            for receipt in page["items"]:
                verify_receipt(receipt, workspace_id)
                if receipt["sequence"] != cursor + 1 or (
                    previous is not None and receipt["previous_hash"] != previous
                ):
                    raise ValueError("Discontinuous chain")
                cursor, previous = receipt["sequence"], receipt["hash"]
            if (
                type(page["next_cursor"]) is not int
                or page["next_cursor"] != cursor
                or (page["has_more"] and not page["items"])
            ):
                raise ValueError("Invalid continuation")
        except (ValueError, KeyError, TypeError) as error:
            raise IntegrityError("Invalid Syn replay page") from error
        return page

    def head(self, workspace_id: str) -> dict[str, Any]:
        value = self._request(workspace_id, "head")
        if (
            set(value) != {"workspace_id", "sequence", "hash"}
            or value["workspace_id"] != workspace_id
            or type(value["sequence"]) is not int
            or value["sequence"] < 0
            or not isinstance(value["hash"], str)
            or len(value["hash"]) != 64
            or any(c not in "0123456789abcdef" for c in value["hash"])
        ):
            raise IntegrityError("Invalid Syn head")
        return value

    def verify(
        self, workspace_id: str, *, expected_head: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # Capture a finite target before reading so concurrent writers cannot extend this run forever.
        target = self.head(workspace_id) if expected_head is None else expected_head
        if (
            target.get("workspace_id") != workspace_id
            or type(target.get("sequence")) is not int
            or target["sequence"] < 0
        ):
            raise IntegrityError("Invalid expected Syn head")
        cursor, previous = 0, GENESIS_HASH
        while cursor < target["sequence"]:
            page = self.messages(
                workspace_id, after=cursor, limit=min(100, target["sequence"] - cursor)
            )
            if not page["items"]:
                raise IntegrityError(
                    "Syn history is truncated before the expected head"
                )
            for receipt in page["items"]:
                if receipt["previous_hash"] != previous:
                    raise IntegrityError("Syn chain does not match its predecessor")
                cursor, previous = receipt["sequence"], receipt["hash"]
        if previous != target.get("hash"):
            raise IntegrityError("Syn history does not match the expected head")
        return {"workspace_id": workspace_id, "sequence": cursor, "hash": previous}
