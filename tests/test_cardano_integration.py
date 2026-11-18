"""Actual TCP/mux -> runtime -> SQLite projections and real dual receipt chains.

The local peer is a synthetic server, not a Cardano consensus implementation.
It sends an unchanged, independently sourced mainnet Conway block. PostgreSQL
domains/triggers and public network interoperability are qualified separately.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import json
from pathlib import Path
import struct

import cbor2
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from syn.cardano import schema as db
from syn.cardano.config import CardanoConfig
from syn.cardano.runtime import CardanoRuntime
from syn.cardano.store import CardanoStore, event_workspace
from syn.communication import heads, messages, metadata as ledger_metadata
from syn.positions.store import workspace_for
from syn_client.protocol import GENESIS_HASH, verify_receipt


FIXTURES = Path(__file__).parent / "fixtures/cardano"
EXPECTED = json.loads((FIXTURES / "conway-v11.json").read_text())
BLOCK = bytes.fromhex((FIXTURES / "conway-v11.block").read_text())
HEADER = BLOCK[EXPECTED["header_offset"]:EXPECTED["header_offset"] + EXPECTED["header_bytes"]]
POINT = [EXPECTED["slot"], bytes.fromhex(EXPECTED["block_hash"])]
# The synthetic peer's anchor uses the fixture's real predecessor hash. Its
# slot is test data, not a claim about the predecessor's mainnet slot.
ANCHOR = [EXPECTED["slot"] - 1, bytes.fromhex(EXPECTED["previous_hash"])]
TIP = [POINT, EXPECTED["block_number"]]
ANCHOR_TIP = [ANCHOR, EXPECTED["block_number"] - 1]
NODE = "syn://thing/cardano-wire-store-observer"
MAGIC = 764824073


class TrustedPeer:
    def __init__(self):
        self.connections = 0
        self.intersections = []
        self.keepalives = 0
        self.block_fetches = 0
        self.errors = []
        self.release_rollback = asyncio.Event()
        self.awaiting = asyncio.Event()
        self.tasks = set()
        self.writers = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        self.connections += 1
        connection = self.connections
        write_lock = asyncio.Lock()
        delayed = None
        requests = 0
        accepted = False
        rolled_back = False

        async def send(protocol, *values, fragment=False):
            payload = b"".join(cbor2.dumps(value) for value in values)
            chunks = (payload[:17], payload[17:]) if fragment else (payload,)
            async with write_lock:
                for chunk in chunks:
                    if chunk:
                        writer.write(struct.pack(">IHH", 0, protocol | 0x8000, len(chunk)) + chunk)
                await writer.drain()

        async def rollback_when_released():
            nonlocal rolled_back
            await self.release_rollback.wait()
            rolled_back = True
            await send(2, [3, ANCHOR, ANCHOR_TIP])

        try:
            while True:
                _, protocol, length = struct.unpack(">IHH", await reader.readexactly(8))
                # Client requests in this fixture fit one mux segment.
                assert protocol in (0, 2, 3, 8) and 0 < length <= 65535
                message = cbor2.loads(await reader.readexactly(length))
                if protocol == 0:
                    assert message[0] == 0 and 14 in message[1]
                    assert message[1][14] == [MAGIC, True, 0, False]
                    await send(0, [1, 14, [MAGIC, True, 0, False]])
                elif protocol == 8:
                    assert message[0] == 0 and len(message) == 2
                    self.keepalives += 1
                    await send(8, [1, message[1]])
                elif protocol == 3:
                    assert message == [0, POINT, POINT]
                    self.block_fetches += 1
                    # A single segment carries three distinct CBOR messages.
                    await send(3, [2], [4, cbor2.CBORTag(24, BLOCK)], [5])
                elif message[0] == 4:
                    offered = message[1]
                    self.intersections.append((connection, offered))
                    if not offered:
                        assert connection == 1, "A restarted feed must use its durable point"
                        await send(2, [6, ANCHOR_TIP])
                    else:
                        assert ANCHOR in offered
                        accepted = True
                        await send(2, [5, ANCHOR, ANCHOR_TIP])
                else:
                    assert message == [0] and accepted
                    requests += 1
                    if requests == 1:
                        # Split one header response across mux segments.
                        await send(2, [2, [6, cbor2.CBORTag(24, HEADER)], TIP], fragment=True)
                    elif requests == 2:
                        await send(2, [1])
                        self.awaiting.set()
                        if connection == 1:
                            delayed = asyncio.create_task(rollback_when_released())
                    elif requests == 3 and connection == 1 and rolled_back:
                        await send(2, [1])
                    else:
                        raise AssertionError("Client requested again while peer had AwaitReply agency")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # Normal runtime stop closes its connection.
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors.append(error)
        finally:
            if delayed is not None:
                delayed.cancel()
                await asyncio.gather(delayed, return_exceptions=True)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.writers.discard(writer)
            self.tasks.discard(task)

    async def close(self):
        for writer in tuple(self.writers):
            writer.close()
        pending = tuple(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def eventually(predicate, runtime, peer):
    try:
        async with asyncio.timeout(8):
            while not predicate():
                assert not peer.errors, peer.errors
                await asyncio.sleep(.01)
    except TimeoutError:
        raise AssertionError(f"Timed out: {runtime.status()!r}; peer errors={peer.errors!r}") from None


def read_chain(sessions, workspace):
    with sessions() as session:
        rows = session.execute(select(messages).where(messages.c.workspace_id == workspace)
            .order_by(messages.c.sequence)).mappings().all()
        head = session.execute(select(heads).where(heads.c.workspace_id == workspace)).mappings().one()
    previous = GENESIS_HASH
    for sequence, row in enumerate(rows, 1):
        receipt = {"schema_version": 1, **dict(row)}
        verify_receipt(receipt, workspace)
        assert receipt["sequence"] == sequence and receipt["previous_hash"] == previous
        previous = receipt["hash"]
    assert head["hash"] == previous and head["sequence"] == len(rows)
    return rows


def test_tcp_block_observation_dual_ledgers_rollback_and_durable_resume(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "cardano-integration.db"))
    db.metadata.create_all(engine)
    ledger_metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        db.seed_types(session)
        session.execute(db.primitive["things"].insert().values(
            id=NODE, name="Integration observer", typeRef="syn://type/device"))
        session.execute(db.position_nodes.insert().values(nodeRef=NODE,
            ledgerWorkspace=workspace_for(NODE), createdAt=datetime.now(UTC), probeReport=[]))
    store = CardanoStore(sessions)

    async def scenario():
        peer = TrustedPeer()
        server = await asyncio.start_server(peer.handle, "127.0.0.1", 0)
        endpoint = "127.0.0.1:" + str(server.sockets[0].getsockname()[1])
        config = CardanoConfig(enabled=True, peers=(endpoint,), timeoutSeconds=2,
                               keepaliveIntervalSeconds=1, reconnectDelaySeconds=.1)
        runtimes = []
        try:
            first = CardanoRuntime(sessions, NODE, config, store=store)
            runtimes.append(first)
            await first.start()
            await eventually(lambda: first.status()["peers"][0].get("active_blocks") == 1,
                             first, peer)
            await asyncio.wait_for(peer.awaiting.wait(), 2)
            assert first.status()["peers"][0]["point"] == {
                "slot": EXPECTED["slot"], "hash": EXPECTED["block_hash"]}
            # Keepalive must work while chain-sync waits with remote agency.
            await eventually(lambda: peer.keepalives >= 2, first, peer)
            peer.release_rollback.set()
            await eventually(lambda: first.status()["peers"][0].get("active_blocks") == 0,
                             first, peer)
            assert first.status()["peers"][0]["rollbacks_received"] == 1
            await first.stop()

            second = CardanoRuntime(sessions, NODE, config, store=store)
            runtimes.append(second)
            await second.start()
            await eventually(lambda: second.status()["peers"][0].get("active_blocks") == 1,
                             second, peer)
            await second.stop()
            assert peer.connections == 2
            assert [points for number, points in peer.intersections if number == 2] == [[ANCHOR]]
            assert peer.block_fetches == 2
            assert not peer.errors
        finally:
            for runtime in runtimes:
                await runtime.stop()
            server.close()
            await server.wait_closed()
            await peer.close()

    try:
        asyncio.run(scenario())
        event_ref = "syn://event/" + EXPECTED["block_hash"]
        with sessions() as session:
            event = session.execute(select(db.primitive["events"])).mappings().one()
            assert event["id"] == event_ref and event["typeRef"] == "syn://type/cardano-block"
            assert event["knowledge"]["slot"] == EXPECTED["slot"]
            assert event["knowledge"]["blockNumber"] == EXPECTED["block_number"]
            assert event["occurredAt"] is None
            records = session.execute(select(db.records).order_by(db.records.c.revision)).mappings().all()
            assert [record["action"] for record in records] == ["apply", "rollback", "apply"]
            assert len({record["emberRef"] for record in records}) == 3
            snapshots = [json.loads(record["snapshot"]) for record in records]
            assert snapshots[1]["knowledge"]["retracts"] == records[0]["emberRef"]
            assert len(session.execute(select(db.primitive["embers"])).all()) == 3
            chronicles = session.execute(select(db.chronicle)).mappings().all()
            assert len(chronicles) == 6
            assert {entry["objectRef"] for entry in chronicles} == {NODE, event_ref}
            assert session.execute(select(db.blocks.c.headerCbor)).scalar_one() == HEADER
        own = read_chain(sessions, workspace_for(NODE))
        event_chain = read_chain(sessions, event_workspace(event_ref))
        observed = [row for row in own if row["payload"].get("profile") == "syn.cardano-observation/1"]
        assert len(own) == 7 and len(event_chain) == len(observed) == 3
        for own_receipt, event_receipt, record in zip(observed, event_chain, records, strict=True):
            assert own_receipt["payload"] == event_receipt["payload"]
            assert own_receipt["message_id"] == event_receipt["message_id"]
            assert record["nodeReceiptHash"] == own_receipt["hash"]
            assert record["eventReceiptHash"] == event_receipt["hash"]
            matching = [entry for entry in chronicles if entry["details"]["emberRef"] == record["emberRef"]]
            assert {entry["details"]["receiptHash"] for entry in matching} == {
                own_receipt["hash"], event_receipt["hash"]}
    finally:
        engine.dispose()
