"""Bounded HTTP(S) pull; never follow remote URLs, redirects, proxies or cookies."""
from __future__ import annotations

import http.client
import socket
import ssl
import time
from urllib.parse import urlsplit

from .protocol import MAX_RESPONSE, strict_json


class PeerUnavailable(OSError):
    def __init__(self, outcome="unavailable"):
        self.outcome = outcome
        super().__init__(outcome)


class PeerClient:
    def __init__(self, peer, timeout=5.0):
        self.peer, self.timeout = peer, timeout

    def get(self, path):
        # Only caller-built protocol paths reach here, never a URL from JSON.
        if not path.startswith("/v1/operator/dag/") or "\r" in path or "\n" in path:
            raise ValueError("Invalid witness request path")
        parsed = urlsplit(self.peer.url)
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                timeout=self.timeout, context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80,
                                                    timeout=self.timeout)
        deadline = time.monotonic() + self.timeout
        try:
            connection.request("GET", path, headers={"Accept": "application/json",
                "Accept-Encoding": "identity", "Connection": "close"})
            active_socket = connection.sock
            response = connection.getresponse()
            if response.status == 409:
                raise PeerUnavailable("no-checkpoint")
            if response.status != 200:
                raise PeerUnavailable()
            if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise ValueError("Compressed witness responses are not accepted")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Peer did not return JSON")
            size = response.getheader("Content-Length")
            if size is not None and (not size.isdecimal() or int(size) > MAX_RESPONSE):
                raise ValueError("Oversized peer response")
            data = bytearray()
            while len(data) <= MAX_RESPONSE:
                if response.isclosed():
                    return strict_json(bytes(data))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PeerUnavailable()
                # HTTPConnection may release sock on Connection: close. The
                # response owns it then; retain the initial per-read timeout.
                if active_socket is not None:
                    active_socket.settimeout(remaining)
                chunk = response.read1(min(4096, MAX_RESPONSE + 1 - len(data)))
                if not chunk:
                    return strict_json(bytes(data))
                data.extend(chunk)
            raise ValueError("Oversized peer response")
        except (TimeoutError, socket.timeout, http.client.HTTPException, ssl.SSLError) as error:
            raise PeerUnavailable() from error
        finally:
            connection.close()

    def checkpoint(self):
        return self.get("/v1/operator/dag/checkpoint")

    def consistency(self, old_size, new_size):
        if type(old_size) is not int or type(new_size) is not int or not 1 <= old_size <= new_size:
            raise ValueError("Invalid checkpoint bounds")
        return self.get(f"/v1/operator/dag/consistency/{old_size}?new_size={new_size}")
