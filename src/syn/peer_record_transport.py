"""Bounded public Operator discovery streams and the public-key IPFS block.

The Operator record is fetched over an authenticated libp2p connection. Bitswap
serves exactly one immutable raw block: the 32-byte Operator base public key.
Neither protocol imports remote authority, journal events, or private material.
"""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import logging
import math
from typing import Any

from google.protobuf.message import DecodeError
from libp2p.bitswap.pb.bitswap_pb2 import Message
from libp2p.custom_types import TProtocol
from libp2p.peer.peerinfo import PeerInfo
import trio

from . import peer_record


logger = logging.getLogger(__name__)
PEER_RECORD_PROTOCOL = TProtocol("/syn/operator/peer-record/1.0.0")
REQUEST_PROFILE = "syn.operator-peer-record-request/1"
BITSWAP_PROTOCOLS = (
    TProtocol("/ipfs/bitswap/1.2.0"),
    TProtocol("/ipfs/bitswap/1.1.0"),
)
MAX_REQUEST_BYTES = 256
MAX_BITSWAP_BYTES = 4 * 1024 * 1024
MAX_WANTS = 256
MAX_CID_BYTES = 512
MAX_MESSAGES = 16
RAW_CID_PREFIX = b"\x01\x55\x12\x20"


def raw_public_key_cid(public_key: bytes) -> bytes:
    """CIDv1(raw, sha2-256(raw base public key)); provider key omits 01 55."""
    if type(public_key) is not bytes or len(public_key) != 32:
        raise ValueError("Expected a raw 32-byte Operator base public key")
    return RAW_CID_PREFIX + hashlib.sha256(public_key).digest()


def _varint(value: int) -> bytes:
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


async def _exact(stream, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = await stream.read(count - len(result))
        if not chunk:
            raise EOFError("Truncated public discovery frame")
        if len(chunk) > count - len(result):
            raise ValueError("Peer stream exceeded the requested read size")
        result.extend(chunk)
    return bytes(result)


async def read_frame(stream, maximum: int) -> bytes:
    """Read a minimally encoded, bounded unsigned-varint length and its payload."""
    length = 0
    prefix = bytearray()
    for shift in range(0, 35, 7):
        value = (await _exact(stream, 1))[0]
        prefix.append(value)
        length |= (value & 127) << shift
        if not value & 128:
            if bytes(prefix) != _varint(length) or length > maximum:
                raise ValueError("Invalid or oversized public discovery frame")
            return await _exact(stream, length)
    raise ValueError("Public discovery frame length is too long")


async def write_frame(stream, data: bytes, maximum: int) -> None:
    if type(data) is not bytes or len(data) > maximum:
        raise ValueError("Oversized public discovery response")
    await stream.write(_varint(len(data)) + data)


async def _finish(stream, *, reset: bool = False) -> None:
    # Cleanup must neither swallow cancellation nor outlive shutdown indefinitely.
    with trio.move_on_after(1, shield=True):
        try:
            if reset:
                await stream.reset()
            else:
                await stream.close()
        except Exception:
            pass


def _request(raw: bytes) -> str:
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError("Duplicate public discovery request field")
            output[key] = value
        return output

    try:
        body = json.loads(raw, object_pairs_hook=pairs)
        if (type(body) is not dict or set(body) != {"profile", "operator"}
                or body["profile"] != REQUEST_PROFILE
                or peer_record.canonical(body) != raw):
            raise ValueError("Invalid public discovery request")
        peer_record.provider_key(body["operator"])
    except (TypeError, UnicodeError, RecursionError) as error:
        raise ValueError("Malformed public discovery request") from error
    return body["operator"]


def _timeout(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 120:
        raise ValueError("Public discovery timeout must be finite and at most 120 seconds")
    return float(value)


class PeerRecordService:
    """Serve already-signed public records without invoking custody per request.

    Register once on the local host. The callback returns a current public
    envelope, or None while an Operator has no publishable endpoint. Concurrent
    inbound streams and each stream's size, work and lifetime are bounded.
    """

    def __init__(self, host, public_key: bytes,
                 record_getter: Callable[[], dict[str, Any] | None], *,
                 timeout: float = 10, max_inbound: int = 16):
        self.host = host
        self.cid = raw_public_key_cid(public_key)
        self.public_key = public_key
        self.operator = hashlib.sha256(public_key).hexdigest()
        self.record_getter = record_getter
        self.timeout = _timeout(timeout)
        if type(max_inbound) is not int or not 1 <= max_inbound <= 64:
            raise ValueError("Public discovery inbound capacity is out of range")
        self._slots = trio.Semaphore(max_inbound)

    def register(self) -> None:
        self.host.set_stream_handler(PEER_RECORD_PROTOCOL, self._serve_record)
        for protocol in BITSWAP_PROTOCOLS:
            async def handle(stream, version=protocol):
                await self._serve_bitswap(stream, version)
            self.host.set_stream_handler(protocol, handle)

    async def _acquire(self, stream) -> bool:
        try:
            self._slots.acquire_nowait()
        except trio.WouldBlock:
            await _finish(stream, reset=True)
            return False
        return True

    async def _serve_record(self, stream) -> None:
        if not await self._acquire(stream):
            return
        failed = True
        try:
            with trio.fail_after(self.timeout):
                requested = _request(await read_frame(stream, MAX_REQUEST_BYTES))
                if requested != self.operator:
                    raise ValueError("Operator record is unavailable")
                packet = self.record_getter()
                if packet is None:
                    raise ValueError("Operator record is unavailable")
                peer_record.verify(packet, expected_operator=self.operator,
                    expected_network=packet["body"]["network_id"],
                    expected_peer_id=str(self.host.get_id()))
                encoded = peer_record.encode(packet)
                await write_frame(stream, encoded, peer_record.MAX_RECORD_BYTES)
                failed = False
        except Exception:
            # Do not expose local record/config contents in remote errors or logs.
            logger.debug("Rejected public Operator record request")
        finally:
            await _finish(stream, reset=failed)
            self._slots.release()

    def _bitswap_response(self, data: bytes, protocol: str) -> Message:
        message = Message()
        try:
            message.ParseFromString(data)
        except DecodeError as error:
            raise ValueError("Malformed Bitswap message") from error
        if len(message.wantlist.entries) > MAX_WANTS:
            raise ValueError("Bitswap wantlist exceeds public-key service capacity")
        # Process final per-CID intent; a later cancellation in the same message
        # must not release a response queued by an earlier entry.
        wants = {}
        for entry in message.wantlist.entries:
            if not 0 < len(entry.block) <= MAX_CID_BYTES:
                raise ValueError("Invalid bounded Bitswap CID")
            if entry.cancel:
                wants.pop(entry.block, None)
            elif entry.wantType in (Message.Wantlist.Block, Message.Wantlist.Have):
                wants[entry.block] = entry
            else:
                raise ValueError("Unknown Bitswap want type")
        response = Message()
        version12 = protocol == BITSWAP_PROTOCOLS[0]
        for cid, entry in sorted(wants.items(), key=lambda item: -item[1].priority):
            if cid == self.cid:
                if version12 and entry.wantType == Message.Wantlist.Have:
                    response.blockPresences.add(cid=cid, type=Message.Have)
                else:
                    response.payload.add(prefix=RAW_CID_PREFIX, data=self.public_key)
            elif version12 and entry.sendDontHave:
                response.blockPresences.add(cid=cid, type=Message.DontHave)
        # Incoming payloads are never persisted or interpreted as identity data.
        return response

    async def _serve_bitswap(self, stream, protocol: str) -> None:
        if not await self._acquire(stream):
            return
        response_stream = None
        failed = True
        try:
            with trio.fail_after(self.timeout):
                for _ in range(MAX_MESSAGES):
                    try:
                        raw = await read_frame(stream, MAX_BITSWAP_BYTES)
                    except EOFError:
                        break
                    response = self._bitswap_response(raw, protocol)
                    if response.payload or response.blockPresences:
                        # Bitswap is message-based: the requester receives blocks
                        # on a new incoming stream, not a request-response stream.
                        if response_stream is None:
                            response_stream = await self.host.new_stream(
                                stream.muxed_conn.peer_id, [TProtocol(protocol)])
                        await write_frame(response_stream, response.SerializeToString(),
                                          MAX_BITSWAP_BYTES)
                failed = False
        except Exception:
            logger.debug("Rejected public-key Bitswap request")
        finally:
            if response_stream is not None:
                await _finish(response_stream, reset=failed)
            await _finish(stream, reset=failed)
            self._slots.release()


async def fetch_peer_record(host, peer_info: PeerInfo, *, expected_operator: str,
                            expected_network: str, timeout: float = 10) -> dict[str, Any]:
    """Fetch and authenticate a candidate; discovery alone grants no authority."""
    peer_record.provider_key(expected_operator)
    request = peer_record.canonical({"profile": REQUEST_PROFILE,
                                     "operator": expected_operator})
    stream = None
    failed = True
    try:
        with trio.fail_after(_timeout(timeout)):
            await host.connect(peer_info)
            stream = await host.new_stream(peer_info.peer_id, [PEER_RECORD_PROTOCOL])
            actual_peer = stream.muxed_conn.peer_id
            if actual_peer != peer_info.peer_id:
                raise ValueError("Public record arrived from another transport peer")
            await write_frame(stream, request, MAX_REQUEST_BYTES)
            packet = peer_record.decode(await read_frame(stream, peer_record.MAX_RECORD_BYTES))
            peer_record.verify(packet, expected_operator=expected_operator,
                expected_network=expected_network, expected_peer_id=str(actual_peer))
            failed = False
            return packet
    finally:
        if stream is not None:
            await _finish(stream, reset=failed)
