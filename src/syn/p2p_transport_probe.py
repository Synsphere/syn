"""Read-only, authenticated public transport connectivity probes for Syn.

A probe demonstrates one libp2p connection using the requested transport and a
fresh application challenge. It does not authenticate a Syn Operator, grant
mesh authority, or prove global reachability from other vantage points.

This module deliberately imports the optional libp2p transport implementations
only when inspecting a live stream. Pure challenge validation is dependency-free.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets

PROTOCOL = "/syn/transport/probe/1.0.0"
_REQUEST_PREFIX = b"syn.transport.probe.request/1\x00"
_RESPONSE_PREFIX = b"syn.transport.probe.response/1\x00"
_DOMAIN = b"syn.transport.probe.digest/1\x00"
KINDS = ("quic-v1", "webrtc-direct", "webtransport")
NONCE_SIZE = 32
MAX_REQUEST = len(_REQUEST_PREFIX) + NONCE_SIZE + 1 + max(map(len, KINDS))
MAX_RESPONSE = len(_RESPONSE_PREFIX) + 32


def _kind(kind: str) -> str:
    if type(kind) is not str or kind not in KINDS:
        raise ValueError("Unsupported probe transport")
    return kind


def _peer_id(value: bytes) -> bytes:
    if type(value) is not bytes or not 3 <= len(value) <= 64:
        raise ValueError("Invalid libp2p peer identifier")
    # The public Syn transport accepts SHA2-256 and compact identity PeerIDs.
    # A randomly sized byte string is not a legitimate transport identity.
    if not ((len(value) == 34 and value[:2] == b"\x12\x20") or
            (value[0] == 0 and 0 < value[1] <= 42 and len(value) == value[1] + 2)):
        raise ValueError("Invalid libp2p peer multihash")
    return value


def make_request(kind: str) -> bytes:
    """Unpredictable, transport-bound challenge; never sends a secret key."""
    return _REQUEST_PREFIX + secrets.token_bytes(NONCE_SIZE) + b"|" + _kind(kind).encode("ascii")


def parse_request(data: bytes) -> tuple[bytes, str]:
    if type(data) is not bytes or not data.startswith(_REQUEST_PREFIX):
        raise ValueError("Malformed transport probe")
    tail = data[len(_REQUEST_PREFIX):]
    if len(tail) < NONCE_SIZE + 2 or tail[NONCE_SIZE:NONCE_SIZE + 1] != b"|":
        raise ValueError("Malformed transport probe")
    try:
        kind = _kind(tail[NONCE_SIZE + 1:].decode("ascii"))
    except UnicodeError:
        raise ValueError("Malformed probe transport") from None
    if len(tail) != NONCE_SIZE + 1 + len(kind):
        raise ValueError("Malformed probe length")
    return tail[:NONCE_SIZE], kind


def response_for(request: bytes, peer_id: bytes, kind: str) -> bytes:
    nonce, requested_kind = parse_request(request)
    if requested_kind != _kind(kind):
        raise ValueError("Probe transport does not match the connection")
    # This is a challenge acknowledgement, not a signature; security comes from
    # the libp2p transport's authenticated handshake and the pinned PeerID.
    digest = hashlib.sha256(_DOMAIN + nonce + _peer_id(peer_id) + kind.encode("ascii")).digest()
    return _RESPONSE_PREFIX + digest


def matches_response(request: bytes, peer_id: bytes, kind: str, response: bytes) -> bool:
    return type(response) is bytes and hmac.compare_digest(
        response_for(request, peer_id, kind), response
    )


def stream_identity(stream) -> tuple[str | None, object | None]:
    """Use concrete upstream connection types, never a peer-provided string."""
    from libp2p.transport.quic.connection import QUICConnection
    from libp2p.transport.webtransport.connection import WebTransportConnection
    from libp2p.transport.webrtc.connection import WebRTCConnection

    mux = getattr(stream, "muxed_conn", None)
    if isinstance(mux, QUICConnection):
        # libp2p QUIC uses TLS 1.3 with authenticated PeerID verification.
        if not mux.is_peer_verified:
            return None, None
        return "quic-v1", mux.remote_peer_id()
    if isinstance(mux, WebRTCConnection):
        # WebRTC Direct verifies its peer over the Noise channel after ICE/DTLS.
        if not mux.is_established:
            return None, None
        return "webrtc-direct", mux.peer_id
    if isinstance(mux, WebTransportConnection):
        # WebTransport uses pinned TLS certificate hashes plus Noise.
        if not mux.is_established:
            return None, None
        return "webtransport", mux.peer_id
    return None, None


def target_kind(address: str) -> str:
    """Require one canonical numeric UDP transport address without peer suffix."""
    import ipaddress
    from multiaddr import Multiaddr

    if type(address) is not str or len(address) > 512:
        raise ValueError("Invalid transport endpoint")
    try:
        addr = Multiaddr(address)
        if str(addr) != address:
            raise ValueError
        parts = [part.name for part in addr.protocols()]
        if parts[:2] not in (["ip4", "udp"], ["ip6", "udp"]):
            raise ValueError
        ip = ipaddress.ip_address(addr.value_for_protocol(parts[0]))
        port = int(addr.value_for_protocol("udp"))
        if (parts[0] != "ip" + str(ip.version) or not 1 <= port <= 65535
                or ip.is_unspecified or ip.is_multicast or ip.is_link_local):
            raise ValueError
        if parts == [parts[0], "udp", "quic-v1"]:
            return "quic-v1"
        if parts == [parts[0], "udp", "webrtc-direct", "certhash"]:
            from libp2p.transport.webrtc.multiaddr_utils import parse_webrtc_direct_multiaddr
            if not parse_webrtc_direct_multiaddr(addr)[2]:
                raise ValueError
            return "webrtc-direct"
        if (len(parts) >= 5 and parts[:4] ==
                [parts[0], "udp", "quic-v1", "webtransport"]
                and all(value == "certhash" for value in parts[4:])):
            from libp2p.transport.webtransport.multiaddr_utils import parse_webtransport_multiaddr
            if not parse_webtransport_multiaddr(addr)[2]:
                raise ValueError
            return "webtransport"
    except Exception:
        raise ValueError("Expected a canonical IP/UDP/QUIC, WebRTC Direct or WebTransport address") from None
    raise ValueError("Unsupported transport address")


class TransportProbeResponder:
    """Register an 8-slot, bounded, non-authoritative connectivity endpoint."""

    def __init__(self, host, *, max_inflight: int = 8, timeout: float = 4.0):
        import trio

        if type(max_inflight) is not int or not 1 <= max_inflight <= 32:
            raise ValueError("Invalid transport probe capacity")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError("Invalid transport probe deadline")
        self.host = host
        self.timeout = float(timeout)
        self.slots = trio.Semaphore(max_inflight)

    def register(self) -> None:
        from libp2p.custom_types import TProtocol
        self.host.set_stream_handler(TProtocol(PROTOCOL), self._handle)

    async def _handle(self, stream) -> None:
        import trio
        from .peer_record_transport import _finish, read_frame, write_frame

        kind, remote_id = stream_identity(stream)
        if kind is None or remote_id is None:
            await _finish(stream, reset=True)
            return
        try:
            self.slots.acquire_nowait()
        except trio.WouldBlock:
            await _finish(stream, reset=True)
            return
        failed = True
        try:
            with trio.fail_after(self.timeout):
                request = await read_frame(stream, MAX_REQUEST)
                response = response_for(request, self.host.get_id().to_bytes(), kind)
                await write_frame(stream, response, MAX_RESPONSE)
                failed = False
        except (EOFError, ValueError, TypeError):
            pass
        finally:
            self.slots.release()
            await _finish(stream, reset=failed)


async def verify_transport(host, peer_info, *, kind: str, timeout: float = 10.0) -> dict:
    """Verify one pinned identity at one address, never silently falling back.

    Caller supplies a fresh host configured only for this UDP transport. Verify
    actual negotiated muxer type after connect and again after new_stream.
    """
    import trio
    from libp2p.custom_types import TProtocol
    from .peer_record_transport import _finish, read_frame, write_frame

    kind = _kind(kind)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("Invalid probe timeout")
    if len(peer_info.addrs) != 1 or target_kind(str(peer_info.addrs[0])) != kind:
        raise ValueError("Probe requires exactly one matching pinned transport address")
    stream = None
    try:
        with trio.fail_after(timeout):
            await host.connect(peer_info)
            stream = await host.new_stream(peer_info.peer_id, [TProtocol(PROTOCOL)])
            actual, remote = stream_identity(stream)
            if actual != kind or remote != peer_info.peer_id:
                raise RuntimeError("Connection used an unexpected or unauthenticated transport")
            request = make_request(kind)
            await write_frame(stream, request, MAX_REQUEST)
            response = await read_frame(stream, MAX_RESPONSE)
            if not matches_response(request, peer_info.peer_id.to_bytes(), kind, response):
                raise ValueError("Transport probe response failed the fresh challenge")
        return {
            "verified": True, "transport": kind, "peerId": str(peer_info.peer_id),
            "endpoint": str(peer_info.addrs[0]), "challengeVerified": True,
            "authenticatedTransport": True, "scope": "witness-vantage-only",
            "operatorIdentityVerified": False, "publicReachabilityProved": False,
        }
    finally:
        if stream is not None:
            await _finish(stream)
