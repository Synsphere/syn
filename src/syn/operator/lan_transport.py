"""Foreground LAN transport for the canonical pending enrollment inbox."""

from __future__ import annotations

import select
import socket
import time
from collections import deque

from syn.agent.lan_enrollment import _signed_advertisement
from syn.agent.lan_transport import (
    HELLO,
    HINT,
    NONCE_BYTES,
    PROBE,
    PROOF,
    PROOF_DOMAIN,
    RECEIPT,
    RECEIPT_DOMAIN,
    SESSION_SECONDS,
    in_scope,
    lan_scope,
    packet_digest,
    receive,
    send,
)
from syn_client.consensus import PolicyError
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes

from .lan_inbox import _intake_locked, _locked


def _signed(server, domain, body):
    return {"body": body, "signature": server.signer.sign(domain + canonical(body)).hex()}


def _session(conn, config_path, host, port):
    deadline = time.monotonic() + SESSION_SECONDS
    hello = exact(
        receive(conn, deadline, limit=256), {"profile", "challenge", "purpose"}, "LAN hello"
    )
    if hello["profile"] != HELLO or hello["purpose"] not in ("discover", "submit"):
        raise SynProtocolError("Unsupported LAN transport hello")
    challenge = hello["challenge"]
    hex_bytes(challenge, NONCE_BYTES, "LAN transport challenge")
    with _locked(config_path) as (server, _):
        proof = _signed(
            server,
            PROOF_DOMAIN,
            {
                "profile": PROOF,
                "challenge": challenge,
                "purpose": hello["purpose"],
                "advertisement": _signed_advertisement(server, {"host": host, "port": port}),
            },
        )
    send(conn, proof, deadline)
    if hello["purpose"] == "discover":
        return
    packet = receive(conn, deadline)
    with _locked(config_path) as (server, inbox):
        pending = _intake_locked(server, inbox, packet, host=host, port=port)
        receipt = _signed(
            server,
            RECEIPT_DOMAIN,
            {
                "profile": RECEIPT,
                "challenge": challenge,
                "packet_sha256": packet_digest(packet),
                "pending": pending,
            },
        )
    send(conn, receipt, deadline)


def _admit(recent, limit):
    now = time.monotonic()
    while recent and recent[0] <= now - 1.0:
        recent.popleft()
    if len(recent) == limit:
        return False
    recent.append(now)
    return True


def serve(config_path, *, host, port, subnet, ready=None, stopped=None):
    """Listen only on the configured broadcast address and private TCP endpoint."""
    network = lan_scope(host, subnet, port)
    with _locked(config_path) as (server, inbox):
        inbox.read()
        _signed_advertisement(server, {"host": host, "port": port})

    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as broadcast,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reply,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
    ):
        broadcast.bind((str(network.broadcast_address), port))
        reply.bind((host, port))
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(8)
        udp_times, tcp_times = deque(), deque()
        if ready is not None:
            ready()
        while stopped is None or not stopped():
            readable, _, _ = select.select([broadcast, listener], [], [], 0.25)
            if broadcast in readable:
                data, peer = broadcast.recvfrom(len(PROBE) + NONCE_BYTES + 1)
                if (
                    in_scope(peer[0], network)
                    and len(data) == len(PROBE) + NONCE_BYTES
                    and data.startswith(PROBE)
                    and _admit(udp_times, 16)
                ):
                    try:
                        reply.sendto(HINT + data[len(PROBE) :], peer)
                    except OSError:
                        pass  # A dropped hint confers no trust or state change.
            if listener in readable:
                conn, peer = listener.accept()
                with conn:
                    if in_scope(peer[0], network) and _admit(tcp_times, 8):
                        try:
                            _session(conn, config_path, host, port)
                        except (OSError, SynProtocolError, PolicyError):
                            # No rejection details or success receipt leave this connection.
                            continue
