"""Independent wire peer plus an upstream mainnet Conway block fixture."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from hashlib import blake2b
import json
from pathlib import Path
import socket
import struct

import cbor2
import pytest

from syn.cardano.protocol import (
    CardanoClient, CardanoProtocolError, Point, RollBackward, RollForward,
    UnsupportedEra, decode_block_header, decode_header,
)


FIXTURES = Path(__file__).parent / "fixtures" / "cardano"
EXPECTED = json.loads((FIXTURES / "conway-v11.json").read_text())
BLOCK = bytes.fromhex((FIXTURES / "conway-v11.block").read_text())
HEADER = BLOCK[EXPECTED["header_offset"]:EXPECTED["header_offset"] + EXPECTED["header_bytes"]]
MAGIC = 764824073
POINT = [EXPECTED["slot"], bytes.fromhex(EXPECTED["block_hash"])]
TIP = [POINT, EXPECTED["block_number"]]


class WirePeer:
    """Test server independently spells out N2N message tags and mux direction."""

    def __init__(self, *, accepted=None, keepalive=True, chain=None, block=BLOCK,
                 handshake_raw=None, fetch_mode="normal"):
        self.accepted = accepted if accepted is not None else [1, 14, [MAGIC, True, 0, False]]
        self.keepalive = keepalive
        self.chain = chain
        self.block = block
        self.handshake_raw = handshake_raw
        self.fetch_mode = fetch_mode
        self.requests = []
        self.keepalives = 0
        self.chain_requests = 0
        self.awaiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.errors = []
        self._tasks = set()
        self._writers = set()
        self._write_lock = asyncio.Lock()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()
        for writer in self._writers:
            writer.close()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        assert not self.errors, self.errors

    async def send(self, writer, protocol, messages, *, fragment=False):
        payload = b"".join(cbor2.dumps(message) for message in messages)
        async with self._write_lock:
            step = 37 if fragment else 65535
            for offset in range(0, len(payload), step):
                part = payload[offset:offset + step]
                frame = struct.pack(">IHH", 123, protocol | 0x8000, len(part)) + part
                # Fragment the TCP frame header too; mux segment boundaries do
                # not necessarily align with TCP reads or CBOR message boundaries.
                writer.write(frame[:3])
                await writer.drain()
                writer.write(frame[3:])
                await writer.drain()

    async def delayed_forward(self, writer):
        await self.release.wait()
        await self.send(writer, 2, [[2, [6, cbor2.CBORTag(24, HEADER)], TIP]], fragment=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        self._writers.add(writer)
        try:
            while True:
                _, protocol, size = struct.unpack(">IHH", await reader.readexactly(8))
                assert protocol in (0, 2, 3, 8)  # initiator direction, no high bit
                message = cbor2.loads(await reader.readexactly(size))
                self.requests.append((protocol, message))
                if protocol == 0:
                    assert message == [0, {v: [MAGIC, True, 0, False] for v in (11, 12, 13, 14)}]
                    if self.handshake_raw is not None:
                        writer.write(self.handshake_raw)
                        await writer.drain()
                    else:
                        await self.send(writer, 0, [self.accepted], fragment=True)
                elif protocol == 8:
                    assert len(message) == 2 and message[0] == 0
                    self.keepalives += 1
                    if self.keepalive:
                        await self.send(writer, 8, [[1, message[1]]])
                elif protocol == 2:
                    if message[0] == 4:
                        if not message[1]:
                            await self.send(writer, 2, [[6, TIP]])
                        else:
                            await self.send(writer, 2, [[5, message[1][0], TIP]])
                    else:
                        assert message == [0]
                        self.chain_requests += 1
                        if self.chain is not None:
                            await self.send(writer, 2, self.chain)
                        elif self.chain_requests == 1:
                            await self.send(writer, 2, [[1]])
                            self.awaiting.set()
                            delayed = asyncio.create_task(self.delayed_forward(writer))
                            self._tasks.add(delayed)
                        else:
                            await self.send(writer, 2, [[3, [], TIP]])
                elif protocol == 3:
                    assert message == [0, POINT, POINT]
                    if self.fetch_mode == "no-blocks":
                        await self.send(writer, 3, [[3]])
                    elif self.fetch_mode == "extra-block":
                        await self.send(writer, 3, [[2], [4, cbor2.CBORTag(24, self.block)],
                                                   [4, cbor2.CBORTag(24, self.block)], [5]])
                    else:
                        # Multiple protocol messages coalesced into one payload.
                        await self.send(writer, 3, [[2], [4, cbor2.CBORTag(24, self.block)], [5]])
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception as error:
            self.errors.append(error)
        finally:
            self.closed.set()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


def test_upstream_conway_header_identity_uses_original_bytes():
    header = decode_block_header(BLOCK)
    assert header.block_hash == EXPECTED["block_hash"]
    assert header.slot == EXPECTED["slot"]
    assert header.block_number == EXPECTED["block_number"]
    assert header.previous_hash == EXPECTED["previous_hash"]
    assert header.era == "conway"
    assert header.cbor == HEADER
    assert decode_header(6, HEADER) == header
    # A legal, longer encoding of the top-level array must hash differently;
    # decoding then reserializing would silently give the wrong block ID.
    noncanonical = b"\x98\x02" + HEADER[1:]
    changed = decode_header(6, noncanonical)
    assert changed.block_hash == blake2b(noncanonical, digest_size=32).hexdigest()
    assert changed.block_hash != EXPECTED["block_hash"]
    assert changed.slot == header.slot


@pytest.mark.parametrize("era", [0, 7, 255, True])
def test_unknown_eras_fail_explicitly(era):
    with pytest.raises(UnsupportedEra):
        decode_header(era, HEADER)


@pytest.mark.parametrize("raw", [HEADER + b"\x00", HEADER[:-1], b"\x9a\xff\xff\xff\xff",
                                    b"\x81" * 66 + b"\x00", b"\x82\xff\x00"])
def test_header_cbor_bounds_trailing_and_malformed_data(raw):
    with pytest.raises(CardanoProtocolError):
        decode_header(6, raw)


def test_wire_tip_await_full_block_rollback_and_keepalive():
    async def run():
        async with WirePeer() as peer:
            async with await CardanoClient.connect("127.0.0.1", peer.port, MAGIC,
                                                   timeout=1, keepalive_interval=.02) as client:
                intersect = await client.intersect_tip()
                assert intersect.point == Point(EXPECTED["slot"], EXPECTED["block_hash"])
                update_task = asyncio.create_task(client.next_update())
                await asyncio.wait_for(peer.awaiting.wait(), 1)
                await asyncio.sleep(.06)
                assert not update_task.done()
                assert peer.chain_requests == 1  # AwaitReply retains server agency.
                assert peer.keepalives >= 2
                peer.release.set()
                update = await asyncio.wait_for(update_task, 1)
                assert isinstance(update, RollForward)
                assert update.header.block_hash == EXPECTED["block_hash"]
                assert update.tip.block_number == EXPECTED["block_number"]
                assert await client.fetch_block(update.header) == BLOCK
                rollback = await client.next_update()
                assert isinstance(rollback, RollBackward)
                assert rollback.point == Point()
                assert peer.chain_requests == 2
            await asyncio.wait_for(peer.closed.wait(), 1)
            assert client._reader_task.done() and client._keepalive_task.done()
    asyncio.run(run())


@pytest.mark.parametrize("accepted", [
    [2, [0, [15]]], [1, 15, [MAGIC, True, 0, False]],
    [1, 14, [1, True, 0, False]], [1, 14, [MAGIC, 0, 0, False]],
    [1, 14, [MAGIC, True, True, False]], [1, 14, [MAGIC, True, 2, False]],
    [1, 14, [MAGIC, True, 0, True]],
    [1, 14, [MAGIC, True]],
])
def test_handshake_rejects_refusal_wrong_network_or_negotiation(accepted):
    async def run():
        async with WirePeer(accepted=accepted) as peer:
            with pytest.raises(CardanoProtocolError):
                await CardanoClient.connect("127.0.0.1", peer.port, MAGIC, timeout=1)
    asyncio.run(run())


@pytest.mark.parametrize("diffusion,sharing", [(True, 0), (False, 0), (True, 1), (False, 1)])
def test_server_capabilities_are_negotiated_without_enabling_responder_or_peer_sharing(diffusion, sharing):
    # Ouroboros Handshake.Client applies acceptableVersion to the server's
    # returned parameters. A duplex-capable peer is compatible with our
    # initiator-only offer; these capability fields are not an equality check.
    async def run():
        async with WirePeer(accepted=[1, 14, [MAGIC, diffusion, sharing, False]]) as peer:
            async with await CardanoClient.connect("127.0.0.1", peer.port, MAGIC, timeout=1) as client:
                assert client.version == 14
                result = await client.intersect_tip()
                assert result.point == Point(EXPECTED["slot"], EXPECTED["block_hash"])
                assert peer.requests[0] == (0, [0, {v: [MAGIC, True, 0, False] for v in (11, 12, 13, 14)}])
                assert {protocol for protocol, _ in peer.requests} <= {0, 2, 8}
    asyncio.run(run())


@pytest.mark.parametrize("reason,detail", [
    ([0, [16, 15]], "No common protocol version; peer supports 15, 16"),
    ([1, 14, "secret\\nremote payload"], "Peer could not decode the offered parameters for version 14"),
    ([2, 14, "secret\\x1b[31m"], "Peer refused the offered parameters for version 14"),
])
def test_handshake_refusal_reports_safe_reason_and_stage(reason, detail):
    async def run():
        async with WirePeer(accepted=[2, reason]) as peer:
            with pytest.raises(CardanoProtocolError) as caught:
                await CardanoClient.connect("127.0.0.1", peer.port, MAGIC, timeout=1)
            assert caught.value.stage == "handshake"
            assert str(caught.value) == detail
            assert "secret" not in str(caught.value)
    asyncio.run(run())


@pytest.mark.parametrize("error,detail", [
    (socket.gaierror(socket.EAI_AGAIN, "private resolver detail"), "DNS lookup failed"),
    (ConnectionRefusedError("private socket detail"), "TCP connection refused"),
    (TimeoutError(), "Timed out waiting for the peer"),
    (OSError("private socket detail"), "Network I/O failed"),
])
def test_connection_failures_report_actionable_bounded_cause(monkeypatch, error, detail):
    async def fail(*args, **kwargs):
        # A stalled IPv6 address must not postpone the IPv4 attempt until after
        # our outer connect timeout. This option belongs to asyncio's native
        # address-family race, not a second Syn reconnect loop.
        assert kwargs["happy_eyeballs_delay"] == .25
        assert kwargs["interleave"] == 1
        raise error
    monkeypatch.setattr(asyncio, "open_connection", fail)
    async def run():
        with pytest.raises(CardanoProtocolError) as caught:
            await CardanoClient.connect("trusted.example", 3001, MAGIC, timeout=1)
        assert caught.value.stage == "connect"
        assert str(caught.value) == detail
        assert caught.value.__cause__ is error
    asyncio.run(run())


def test_silent_handshake_is_reported_as_handshake_timeout_and_closes_connection():
    async def run():
        async with WirePeer(handshake_raw=b"") as peer:
            with pytest.raises(CardanoProtocolError) as caught:
                await CardanoClient.connect("127.0.0.1", peer.port, MAGIC, timeout=.1)
            assert caught.value.stage == "handshake"
            assert str(caught.value) == "Timed out waiting for the peer"
            await asyncio.wait_for(peer.closed.wait(), 1)
    asyncio.run(run())


@pytest.mark.parametrize("raw", [
    struct.pack(">IHH", 0, 0, 1) + b"\x00",  # wrong direction
    struct.pack(">IHH", 0, 0x8009, 1) + b"\x00",  # unsupported protocol
    struct.pack(">IHH", 0, 0x8000, 0),
    struct.pack(">IHH", 0, 0x8000, 65535),  # length exceeds handshake bound
    struct.pack(">IHH", 0, 0x8000, 5) + b"\x9a\xff\xff\xff\xff",
])
def test_mux_rejects_bad_direction_protocol_size_and_cbor(raw):
    async def run():
        async with WirePeer(handshake_raw=raw) as peer:
            with pytest.raises(CardanoProtocolError):
                await CardanoClient.connect("127.0.0.1", peer.port, MAGIC, timeout=1)
    asyncio.run(run())


@pytest.mark.parametrize("messages", [
    [[1], [1]], [[4, []]], [[2, [7, cbor2.CBORTag(24, HEADER)], TIP]],
    [[2, [6, HEADER], TIP]], [[3, [1, b"short"], TIP]], [[3, [], [[], True]]],
])
def test_chain_sync_rejects_wrong_agency_shapes_and_era(messages):
    async def run():
        async with WirePeer(chain=messages) as peer:
            async with await CardanoClient.connect("127.0.0.1", peer.port, MAGIC, timeout=1) as client:
                with pytest.raises(CardanoProtocolError):
                    await client.next_update()
    asyncio.run(run())


@pytest.mark.parametrize("mode,block", [
    ("no-blocks", BLOCK), ("extra-block", BLOCK),
    ("normal", BLOCK[:-1]), ("normal", BLOCK[:3] + b"\x98\x02" + BLOCK[4:]),
])
def test_block_fetch_requires_one_complete_matching_block(mode, block):
    async def run():
        async with WirePeer(fetch_mode=mode, block=block) as peer:
            async with await CardanoClient.connect("127.0.0.1", peer.port, MAGIC, timeout=1) as client:
                with pytest.raises(CardanoProtocolError):
                    await client.fetch_block(decode_header(6, HEADER))
    asyncio.run(run())


def test_keepalive_failure_wakes_chain_await():
    async def run():
        async with WirePeer(keepalive=False) as peer:
            async with await CardanoClient.connect("127.0.0.1", peer.port, MAGIC,
                                                   timeout=.1, keepalive_interval=.02) as client:
                with pytest.raises(CardanoProtocolError) as caught:
                    await asyncio.wait_for(client.next_update(), 1)
                assert peer.awaiting.is_set()
                assert caught.value.stage == "keepalive"
                assert str(caught.value) == "Timed out waiting for the peer"
    asyncio.run(run())


def test_cancellation_closes_agency_in_flight_without_background_tasks():
    async def run():
        async with WirePeer() as peer:
            async with await CardanoClient.connect("127.0.0.1", peer.port, MAGIC, timeout=1) as client:
                task = asyncio.create_task(client.next_update())
                await asyncio.wait_for(peer.awaiting.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert client.writer.is_closing()
            assert client._reader_task.done() and client._keepalive_task.done()
    asyncio.run(run())
