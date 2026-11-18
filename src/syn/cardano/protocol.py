"""Small initiator-only Ouroboros node-to-node client for trusted peers.

Implements mux framing, N2N handshake v11–14, non-pipelined chain-sync,
single-block fetch and keepalive. It checks wire structure and block identity;
it does not validate Cardano consensus, KES/VRF proofs or transactions.

Wire reference: txpipe/pallas at daecef59a1979d89515524d9583cc28655af182c,
``pallas-network/src/{multiplexer,miniprotocols/*/codec}.rs`` and
``pallas-traverse/src/{header,hashes}.rs``. Oura's N2N source uses the same
header-followed-by-block-fetch flow. No Pallas/Oura runtime is required.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from hashlib import blake2b
import math
import re
import secrets
import socket
import struct
import time

import cbor2


MAX_HEADER_BYTES = 64 * 1024
MAX_BLOCK_BYTES = 4 * 1024 * 1024
MAX_CBOR_DEPTH = 64
MAX_CBOR_ITEMS = 1_000_000
VERSIONS = (11, 12, 13, 14)
ERAS = {1: "shelley", 2: "allegra", 3: "mary", 4: "alonzo", 5: "babbage", 6: "conway"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LIMITS = {0: 16 * 1024, 2: MAX_HEADER_BYTES + 4096,
           3: MAX_BLOCK_BYTES + 4096, 8: 1024}


class CardanoProtocolError(RuntimeError):
    """Connection, agency, encoding or block-identity failure."""

    def __init__(self, message, *, stage=None):
        super().__init__(message)
        self.stage = stage


class UnsupportedEra(CardanoProtocolError):
    """An era whose header format this small follower does not implement."""


def _protocol_error(error, stage):
    """Keep actionable local causes without reflecting socket/peer payload text."""
    if isinstance(error, CardanoProtocolError):
        if error.stage is None:
            error.stage = stage
        return error
    if isinstance(error, socket.gaierror):
        reason = "DNS lookup failed"
    elif isinstance(error, TimeoutError):
        reason = "Timed out waiting for the peer"
    elif isinstance(error, ConnectionRefusedError):
        reason = "TCP connection refused"
    elif isinstance(error, (ConnectionResetError, BrokenPipeError)):
        reason = "Peer reset the connection"
    elif isinstance(error, asyncio.IncompleteReadError):
        reason = "Peer closed the connection before its response completed"
    elif isinstance(error, OSError):
        reason = "Network I/O failed"
    else:
        reason = "Cardano peer operation failed"
    result = CardanoProtocolError(reason, stage=stage)
    result.__cause__ = error
    return result


def _handshake_refusal(message):
    """Decode the refusal category, never a peer-supplied free-form error string."""
    _, reason = _array(message, 2)
    if type(reason) is not list or not reason or type(reason[0]) is not int:
        raise CardanoProtocolError("Malformed handshake refusal")
    if reason[0] == 0:
        _, versions = _array(reason, 2)
        if (type(versions) is not list or len(versions) > 32
                or any(type(version) is not int or not 0 <= version <= 65535 for version in versions)):
            raise CardanoProtocolError("Malformed handshake version list")
        offered = ", ".join(str(version) for version in sorted(set(versions))) or "none"
        return CardanoProtocolError("No common protocol version; peer supports " + offered)
    if reason[0] in (1, 2):
        _, version, detail = _array(reason, 3)
        _uint(version, 65535)
        if type(detail) is not str:
            raise CardanoProtocolError("Malformed handshake refusal detail")
        label = "Peer could not decode the offered parameters" if reason[0] == 1 else "Peer refused the offered parameters"
        return CardanoProtocolError(f"{label} for version {version}")
    return CardanoProtocolError("Cardano node refused the handshake")


def _uint(value, maximum=(1 << 64) - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise CardanoProtocolError("Expected a bounded unsigned integer")
    return value


@dataclass(frozen=True)
class Point:
    slot: int | None = None
    hash: str | None = None

    def __post_init__(self):
        if self.slot is None and self.hash is None:
            return
        _uint(self.slot)
        if type(self.hash) is not str or _HASH.fullmatch(self.hash) is None:
            raise CardanoProtocolError("Invalid Cardano point hash")

    def to_cbor(self):
        return [] if self.slot is None else [self.slot, bytes.fromhex(self.hash)]


@dataclass(frozen=True)
class Tip:
    point: Point
    block_number: int


@dataclass(frozen=True)
class Intersect:
    point: Point | None
    tip: Tip


@dataclass(frozen=True)
class Header:
    block_hash: str
    slot: int
    block_number: int
    previous_hash: str | None
    era: str
    cbor: bytes

    @property
    def point(self):
        return Point(self.slot, self.block_hash)


@dataclass(frozen=True)
class RollForward:
    header: Header
    tip: Tip


@dataclass(frozen=True)
class RollBackward:
    point: Point
    tip: Tip


class _Incomplete(Exception):
    pass


def _head(raw, offset):
    if offset >= len(raw):
        raise _Incomplete
    initial = raw[offset]
    major, extra = initial >> 5, initial & 31
    offset += 1
    if extra < 24:
        return major, extra, offset
    if extra == 31:
        return major, None, offset
    if extra > 27:
        raise CardanoProtocolError("Invalid CBOR argument")
    size = 1 << (extra - 24)
    if offset + size > len(raw):
        raise _Incomplete
    return major, int.from_bytes(raw[offset:offset + size], "big"), offset + size


def _cbor_end(raw, start=0):
    """Find one complete CBOR item without decoding or allocating its contents.

    A preflight scan bounds nesting/declared collection sizes before cbor2.
    Its offsets also preserve original header bytes; cbor2's buffered decoder
    can read ahead and its underlying file position is not an item boundary.
    """
    items = 0

    def scan(offset, depth):
        nonlocal items
        items += 1
        if depth > MAX_CBOR_DEPTH or items > MAX_CBOR_ITEMS:
            raise CardanoProtocolError("CBOR nesting or item limit exceeded")
        major, size, offset = _head(raw, offset)
        if size is None:
            if major not in (2, 3, 4, 5):
                raise CardanoProtocolError("Invalid indefinite CBOR item")
            count = 0
            while True:
                if offset >= len(raw):
                    raise _Incomplete
                if raw[offset] == 255:
                    if major == 5 and count % 2:
                        raise CardanoProtocolError("Incomplete CBOR map pair")
                    return offset + 1
                if major in (2, 3):
                    chunk_major, chunk_size, _ = _head(raw, offset)
                    if chunk_major != major or chunk_size is None:
                        raise CardanoProtocolError("Invalid CBOR string chunk")
                offset = scan(offset, depth + 1)
                count += 1
        if major in (0, 1, 7):
            return offset
        if major in (2, 3):
            if size > MAX_BLOCK_BYTES + 4096:
                raise CardanoProtocolError("CBOR string exceeds byte limit")
            if offset + size > len(raw):
                raise _Incomplete
            return offset + size
        if major == 6:
            return scan(offset, depth + 1)
        count = size if major == 4 else 2 * size
        if count > MAX_CBOR_ITEMS:
            raise CardanoProtocolError("CBOR collection exceeds item limit")
        for _ in range(count):
            offset = scan(offset, depth + 1)
        return offset

    return scan(start, 0)


def _complete(raw, maximum):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise CardanoProtocolError("CBOR document exceeds byte limit")
    try:
        if _cbor_end(raw) != len(raw):
            raise CardanoProtocolError("Trailing CBOR data")
    except _Incomplete:
        raise CardanoProtocolError("Truncated CBOR document") from None


def _decode(raw, maximum):
    _complete(raw, maximum)
    try:
        return cbor2.loads(raw)
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise CardanoProtocolError("Invalid CBOR document") from error


def _array(value, size):
    if type(value) is not list or len(value) != size:
        raise CardanoProtocolError("Unexpected protocol message shape")
    return value


def _point(value):
    if type(value) is list and not value:
        return Point()
    slot, digest = _array(value, 2)
    if type(digest) is not bytes or len(digest) != 32:
        raise CardanoProtocolError("Invalid point digest")
    return Point(_uint(slot), digest.hex())


def _tip(value):
    point, number = _array(value, 2)
    return Tip(_point(point), _uint(number))


def _tagged_bytes(value, maximum):
    if not isinstance(value, cbor2.CBORTag) or value.tag != 24:
        raise CardanoProtocolError("Expected tag-24 CBOR payload")
    raw = value.value
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise CardanoProtocolError("Invalid CBOR payload length")
    return raw


def decode_header(era: int, raw: bytes) -> Header:
    """Decode Shelley–Conway metadata; hash the exact received header bytes."""
    if type(era) is not int or era not in ERAS:
        raise UnsupportedEra("Only Shelley through Conway block headers are supported")
    body, signature = _array(_decode(raw, MAX_HEADER_BYTES), 2)
    _array(body, 15 if era <= 4 else 10)
    if type(signature) is not bytes:
        raise CardanoProtocolError("Invalid header signature encoding")
    number, slot, previous = _uint(body[0]), _uint(body[1]), body[2]
    if previous is not None and (type(previous) is not bytes or len(previous) != 32):
        raise CardanoProtocolError("Invalid previous block hash")
    return Header(blake2b(raw, digest_size=32).hexdigest(), slot, number,
                  None if previous is None else previous.hex(), ERAS[era], raw)


def decode_block_header(raw: bytes) -> Header:
    """Extract the unchanged header from a full hard-fork block CBOR wrapper."""
    _complete(raw, MAX_BLOCK_BYTES)
    major, length, offset = _head(raw, 0)
    if (major, length) != (4, 2):
        raise CardanoProtocolError("Invalid multi-era block wrapper")
    era_major, wrapper_era, offset = _head(raw, offset)
    if era_major != 0 or wrapper_era not in range(2, 8):
        raise UnsupportedEra("Only Shelley through Conway blocks are supported")
    body_major, body_length, offset = _head(raw, offset)
    if body_major != 4 or body_length not in (4, 5):
        raise CardanoProtocolError("Invalid multi-era block body")
    end = _cbor_end(raw, offset)
    return decode_header(wrapper_era - 1, raw[offset:end])


class CardanoClient:
    """One trusted TCP peer; callers serialize chain-sync through this client.

    ``next_update`` waits through AwaitReply without issuing a second request.
    Keepalive failure terminates that wait. No peer sharing, transaction gossip,
    server mode, historical Byron parsing or independent consensus is provided.
    """

    def __init__(self, reader, writer, timeout, keepalive_interval):
        self.reader, self.writer = reader, writer
        self.timeout, self.keepalive_interval = timeout, keepalive_interval
        self.version = None
        self._failure = asyncio.get_running_loop().create_future()
        self._queues = {protocol: asyncio.Queue(maxsize=4) for protocol in _LIMITS}
        self._write_lock, self._chain_lock, self._block_lock = (asyncio.Lock() for _ in range(3))
        self._started = time.monotonic()
        self._reader_task = self._keepalive_task = None

    @classmethod
    async def connect(cls, host: str, port: int, network_magic: int, *,
                      timeout: float = 15, keepalive_interval: float = 20):
        if type(host) is not str or not host or len(host) > 253:
            raise ValueError("Expected a trusted node host")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Expected a TCP port between 1 and 65535")
        _uint(network_magic, (1 << 32) - 1)
        for value in (timeout, keepalive_interval):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
                raise ValueError("Expected a finite positive protocol interval at most 300 seconds")
        try:
            async with asyncio.timeout(timeout):
                # Race address families so an unusable IPv6 route does not
                # consume the whole timeout before a working IPv4 is attempted.
                reader, writer = await asyncio.open_connection(
                    host, port, limit=65536, happy_eyeballs_delay=.25, interleave=1)
        except (OSError, TimeoutError) as error:
            raise _protocol_error(error, "connect") from error
        client = cls(reader, writer, timeout, keepalive_interval)
        client._reader_task = asyncio.create_task(client._read_loop())
        try:
            async with asyncio.timeout(timeout):
                await client._send(0, [0, {v: [network_magic, True, 0, False] for v in VERSIONS}])
                message = await client._receive(0)
                if type(message) is list and message and type(message[0]) is int and message[0] == 2:
                    raise _handshake_refusal(message)
                code, version, data = _array(message, 3)
                if type(code) is not int or code != 1 or type(version) is not int or version not in VERSIONS:
                    raise CardanoProtocolError("Invalid accepted protocol version")
                magic, initiator_only, peer_sharing, query = _array(data, 4)
                # The client applies acceptableVersion to the returned data,
                # as Ouroboros' Handshake.Client does. A remote duplex/peer-
                # sharing capability cannot turn our initiator-only connection
                # into a responder or enable peer discovery. Both booleans and
                # the peer-sharing enum still require their exact wire types.
                if (_uint(magic, (1 << 32) - 1) != network_magic or type(initiator_only) is not bool
                        or type(peer_sharing) is not int or peer_sharing not in (0, 1) or query is not False):
                    raise CardanoProtocolError("Incompatible negotiated network or diffusion mode")
                client.version = version
            client._keepalive_task = asyncio.create_task(client._keep_alive())
            return client
        except asyncio.CancelledError:
            await client.close()
            raise
        except Exception as error:
            await client.close()
            failure = _protocol_error(error, "handshake")
            if failure is error:
                raise
            raise failure from error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    def _fail(self, error, stage=None):
        error = _protocol_error(error, stage)
        if not self._failure.done():
            self._failure.set_result(error)
        self.writer.close()

    async def close(self):
        self._fail(CardanoProtocolError("Cardano connection closed"))
        tasks = [t for t in (self._reader_task, self._keepalive_task)
                 if t is not None and t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(Exception):
            async with asyncio.timeout(1):
                await self.writer.wait_closed()

    async def _read_loop(self):
        buffers = {protocol: bytearray() for protocol in _LIMITS}
        try:
            while True:
                _, wire_protocol, length = struct.unpack(">IHH", await self.reader.readexactly(8))
                protocol = wire_protocol ^ 0x8000
                if protocol not in _LIMITS or not length:
                    raise CardanoProtocolError("Unexpected mux protocol or empty segment")
                buffer = buffers[protocol]
                if len(buffer) + length > _LIMITS[protocol]:
                    raise CardanoProtocolError("Mux message exceeds its byte limit")
                buffer.extend(await self.reader.readexactly(length))
                while buffer:
                    try:
                        end = _cbor_end(buffer)
                    except _Incomplete:
                        break
                    raw = bytes(buffer[:end])
                    del buffer[:end]
                    message = _decode(raw, _LIMITS[protocol])
                    try:
                        self._queues[protocol].put_nowait(message)
                    except asyncio.QueueFull:
                        raise CardanoProtocolError("Unsolicited protocol message overflow") from None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error, "handshake" if self.version is None else "mux")

    async def _send(self, protocol, value):
        if self._failure.done():
            raise self._failure.result()
        data = cbor2.dumps(value)
        async with self._write_lock:
            for offset in range(0, len(data), 65535):
                chunk = data[offset:offset + 65535]
                timestamp = int((time.monotonic() - self._started) * 1_000_000) & 0xffffffff
                self.writer.write(struct.pack(">IHH", timestamp, protocol, len(chunk)) + chunk)
            await self.writer.drain()

    async def _receive(self, protocol):
        task = asyncio.create_task(self._queues[protocol].get())
        try:
            await asyncio.wait((task, self._failure), return_when=asyncio.FIRST_COMPLETED)
            if self._failure.done():
                raise self._failure.result()
            return task.result()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _keep_alive(self):
        try:
            while True:
                cookie = secrets.randbelow(65536)
                async with asyncio.timeout(self.timeout):
                    await self._send(8, [0, cookie])
                    code, returned = _array(await self._receive(8), 2)
                    if type(code) is not int or code != 1 or _uint(returned, 65535) != cookie:
                        raise CardanoProtocolError("Invalid keepalive reply")
                await asyncio.sleep(self.keepalive_interval)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error, "keepalive")

    async def find_intersect(self, points: list[Point]) -> Intersect:
        if type(points) is not list or len(points) > 256 or any(type(p) is not Point for p in points):
            raise ValueError("Expected at most 256 Cardano intersection points")
        async with self._chain_lock:
            try:
                async with asyncio.timeout(self.timeout):
                    await self._send(2, [4, [p.to_cbor() for p in points]])
                    message = await self._receive(2)
                    if type(message) is list and message and type(message[0]) is int:
                        if message[0] == 5:
                            _, point, tip = _array(message, 3)
                            point = _point(point)
                            if point not in points:
                                raise CardanoProtocolError("Peer selected an unoffered intersection")
                            return Intersect(point, _tip(tip))
                        if message[0] == 6:
                            _, tip = _array(message, 2)
                            return Intersect(None, _tip(tip))
                    raise CardanoProtocolError("Invalid intersection response")
            except BaseException as error:
                self._fail(error, "chain-sync")
                if not isinstance(error, Exception):
                    raise
                failure = self._failure.result()
                if failure is error:
                    raise
                raise failure from error

    async def intersect_tip(self) -> Intersect:
        tip = (await self.find_intersect([])).tip
        result = await self.find_intersect([tip.point])
        if result.point is None:
            self._fail(CardanoProtocolError("Chain tip changed during intersection"), "chain-sync")
            raise self._failure.result()
        return result

    async def next_update(self) -> RollForward | RollBackward:
        async with self._chain_lock:
            try:
                async with asyncio.timeout(self.timeout):
                    await self._send(2, [0])
                    message = await self._receive(2)
                if message == [1] and type(message[0]) is int:
                    # The peer now has MustReply agency; do not send RequestNext.
                    message = await self._receive(2)
                code, content, tip = _array(message, 3)
                if type(code) is not int:
                    raise CardanoProtocolError("Invalid chain-sync response")
                tip = _tip(tip)
                if code == 2:
                    era, payload = _array(content, 2)
                    if type(era) is not int or era not in ERAS:
                        raise UnsupportedEra("Only Shelley through Conway headers are supported")
                    header = decode_header(era, _tagged_bytes(payload, MAX_HEADER_BYTES))
                    return RollForward(header, tip)
                if code == 3:
                    return RollBackward(_point(content), tip)
                raise CardanoProtocolError("Invalid chain-sync response")
            except BaseException as error:
                self._fail(error, "chain-sync")
                if not isinstance(error, Exception):
                    raise
                failure = self._failure.result()
                if failure is error:
                    raise
                raise failure from error

    async def fetch_block(self, header: Header) -> bytes:
        async with self._block_lock:
            try:
                async with asyncio.timeout(self.timeout):
                    point = header.point.to_cbor()
                    await self._send(3, [0, point, point])
                    if await self._receive(3) != [2]:
                        raise CardanoProtocolError("Peer cannot fetch the announced block")
                    code, payload = _array(await self._receive(3), 2)
                    if type(code) is not int or code != 4:
                        raise CardanoProtocolError("Expected one fetched block")
                    raw = _tagged_bytes(payload, MAX_BLOCK_BYTES)
                    if await self._receive(3) != [5]:
                        raise CardanoProtocolError("Invalid end of single-block batch")
                    fetched = decode_block_header(raw)
                    if fetched != header:
                        raise CardanoProtocolError("Fetched block does not match announced header")
                    return raw
            except BaseException as error:
                self._fail(error, "block-fetch")
                if not isinstance(error, Exception):
                    raise
                failure = self._failure.result()
                if failure is error:
                    raise
                raise failure from error
