"""Opt-in, subnet-scoped LAN discovery and authenticated pledge delivery."""

from __future__ import annotations

import hashlib
import secrets
import socket
import struct
import time
from ipaddress import IPv4Address, IPv4Network

from syn_client.consensus import PolicyError
from syn_client.custody import verify_ed25519
from syn_client.lan_enrollment import PRIVATE_NETWORKS, private_endpoint, verify_advertisement
from syn_client.operator_protocol import SynProtocolError, canonical, exact, hex_bytes, parse

PROBE = b"SYN-LAN/1?"
HINT = b"SYN-LAN/1!"
NONCE_BYTES = 32
HELLO = "syn.lan-transport-hello/1"
PROOF = "syn.lan-transport-proof/1"
RECEIPT = "syn.lan-transport-receipt/1"
PROOF_DOMAIN = b"syn.lan-transport-proof.v1\0"
RECEIPT_DOMAIN = b"syn.lan-transport-receipt.v1\0"
FRAME_LIMIT = 16 * 1024
SESSION_SECONDS = 4.0
PROBE_SECONDS = 2.0
CANDIDATE_LIMIT = 4


def lan_scope(address, subnet, port):
    private_endpoint(address, port)
    try:
        network = IPv4Network(subnet, strict=True)
    except ValueError:
        raise SynProtocolError("LAN transport requires a canonical private IPv4 subnet") from None
    if (
        str(network) != subnet
        or network.prefixlen > 30
        or not any(network.subnet_of(private) for private in PRIVATE_NETWORKS)
        or not in_scope(address, network)
    ):
        raise SynProtocolError("LAN transport address is outside the permitted private subnet")
    return network


def in_scope(host, network):
    address = IPv4Address(host)
    return address in network and address not in {
        network.network_address,
        network.broadcast_address,
    }


def set_deadline(conn, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("LAN transport deadline expired")
    conn.settimeout(remaining)


def _read(conn, count, deadline):
    data = bytearray()
    while len(data) < count:
        set_deadline(conn, deadline)
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise SynProtocolError("LAN transport closed before the frame was complete")
        data.extend(chunk)
    return bytes(data)


def receive(conn, deadline, *, limit=FRAME_LIMIT):
    count = struct.unpack(">I", _read(conn, 4, deadline))[0]
    if not 0 < count <= limit:
        raise SynProtocolError("LAN transport frame length rejected")
    return parse(_read(conn, count, deadline))


def send(conn, value, deadline):
    data = canonical(value)
    if not 0 < len(data) <= FRAME_LIMIT:
        raise SynProtocolError("LAN transport frame length rejected")
    set_deadline(conn, deadline)
    conn.sendall(struct.pack(">I", len(data)) + data)


def packet_digest(packet):
    return hashlib.sha256(canonical(packet)).hexdigest()


def _verified_route(config, advertisement, host, port):
    return verify_advertisement(
        advertisement,
        expected_operator=config["expected_operator"],
        network_id=config["network_id"],
        host=host,
        port=port,
        now=int(time.time()),
    )


def _verify_signed(value, public, domain):
    signed = exact(value, {"body", "signature"}, "signed LAN transport message")
    verify_ed25519(
        public,
        hex_bytes(signed["signature"], 64, "LAN signature"),
        domain + canonical(signed["body"]),
    )
    return signed["body"]


def _authenticate(conn, config, endpoint, purpose, deadline):
    challenge = secrets.token_hex(NONCE_BYTES)
    send(conn, {"profile": HELLO, "challenge": challenge, "purpose": purpose}, deadline)
    signed = receive(conn, deadline)
    proof = exact(
        exact(signed, {"body", "signature"}, "signed LAN transport proof")["body"],
        {"profile", "challenge", "purpose", "advertisement"},
        "LAN transport proof",
    )
    if (proof["profile"], proof["challenge"], proof["purpose"]) != (PROOF, challenge, purpose):
        raise SynProtocolError("LAN proof does not answer this connection challenge")
    route = _verified_route(config, proof["advertisement"], *endpoint)
    public = hex_bytes(
        route["pulse"]["body"]["signed_pulse"]["operator_public_key"], 32, "operator public key"
    )
    _verify_signed(signed, public, PROOF_DOMAIN)
    return proof["advertisement"], public, challenge


def _connect(conn, address, endpoint, deadline):
    conn.bind((address, 0))
    set_deadline(conn, deadline)
    conn.connect(endpoint)


def discover(config, *, local_address, subnet, port):
    """Find a route with a fresh challenge proof from the independently pinned operator."""
    network = lan_scope(local_address, subnet, port)
    nonce = secrets.token_bytes(NONCE_BYTES)
    seen = set()
    probe_deadline = time.monotonic() + PROBE_SECONDS
    deadline = probe_deadline + SESSION_SECONDS
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.bind((local_address, 0))
        udp.sendto(PROBE + nonce, (str(network.broadcast_address), port))
        while len(seen) < CANDIDATE_LIMIT:
            try:
                set_deadline(udp, probe_deadline)
                response, endpoint = udp.recvfrom(len(HINT) + NONCE_BYTES + 1)
            except TimeoutError:
                break
            host, reply_port = endpoint
            if (
                response != HINT + nonce
                or reply_port != port
                or host in seen
                or not in_scope(host, network)
            ):
                continue
            seen.add(host)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
                    attempt_deadline = min(deadline, time.monotonic() + 1.0)
                    _connect(conn, local_address, endpoint, attempt_deadline)
                    return _authenticate(conn, config, endpoint, "discover", attempt_deadline)[0]
            except (OSError, SynProtocolError, PolicyError):
                continue
    raise SynProtocolError("No authenticated Syn operator answered on the permitted LAN")


def deliver(config, packet, *, local_address, subnet):
    """Send an already committed encrypted pledge; require a signed pending receipt."""
    from .lan_enrollment import SUBMISSION_PROFILE

    submission = exact(packet, {"profile", "route", "offer"}, "LAN pledge submission")
    if submission["profile"] != SUBMISSION_PROFILE:
        raise SynProtocolError("Unknown LAN pledge submission profile")
    signed_route = exact(submission["route"], {"advertisement", "signature"}, "signed LAN route")
    route = exact(signed_route["advertisement"], {"profile", "endpoint", "pulse"}, "LAN route")
    endpoint = exact(route["endpoint"], {"host", "port"}, "LAN route endpoint")
    host, port = endpoint["host"], endpoint["port"]
    network = lan_scope(local_address, subnet, port)
    private_endpoint(host, port)
    if not in_scope(host, network):
        raise SynProtocolError("LAN pledge route is outside the permitted subnet")
    _verified_route(config, submission["route"], host, port)
    digest = packet_digest(packet)
    deadline = time.monotonic() + SESSION_SECONDS
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
        _connect(conn, local_address, (host, port), deadline)
        _, public, challenge = _authenticate(conn, config, (host, port), "submit", deadline)
        send(conn, packet, deadline)
        signed = receive(conn, deadline)
    body = exact(
        _verify_signed(signed, public, RECEIPT_DOMAIN),
        {"profile", "challenge", "packet_sha256", "pending"},
        "LAN pending receipt",
    )
    if (body["profile"], body["challenge"], body["packet_sha256"]) != (RECEIPT, challenge, digest):
        raise SynProtocolError("LAN receipt does not acknowledge this submission")
    pending = exact(
        body["pending"],
        {"profile", "pledge_offer_sha256", "pending_persisted", "agent_activation_authorized"},
        "pending pledge result",
    )
    hex_bytes(pending["pledge_offer_sha256"], 32, "pending pledge offer digest")
    if (
        pending["profile"] != "syn.lan-pledge-pending/1"
        or pending["pending_persisted"] is not True
        or pending["agent_activation_authorized"] is not False
    ):
        raise SynProtocolError("LAN receipt does not confirm pending-only persistence")
    return pending
