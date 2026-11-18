"""Follower lifecycle, durable resume and transaction completion boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import threading

import pytest
from sqlalchemy.exc import SQLAlchemyError

from syn.cardano.config import CardanoConfig
from syn.cardano.protocol import (
    CardanoProtocolError, Header, Intersect, Point, RollBackward, RollForward,
    Tip, UnsupportedEra,
)
from syn.cardano.runtime import CardanoRuntime
from syn.cardano.store import CardanoStoreConflict


NODE = "syn://thing/cardano-test-observer"
PEER = "trusted-a.example:3001"
SECOND = "trusted-b.example:3002"
BASE = Point(10, "a" * 64)
HEAD = Header("b" * 64, 11, 2, "a" * 64, "conway", b"header")
TIP = Tip(HEAD.point, 2)


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


def config(peers=(PEER,), **kwargs):
    return CardanoConfig(enabled=True, peers=peers, reconnectDelaySeconds=.1,
                         maxReconnectDelaySeconds=.1, **kwargs)


class Store:
    """Synchronous transaction boundary with a visible committed checkpoint."""

    def __init__(self, *, saved=None, fail_apply=None, order=None):
        self.saved = deepcopy(saved or {})
        self.fail_apply = fail_apply
        self.order = order if order is not None else []
        self.prepared = []
        self.intersections = []
        self.applied = []
        self.rollbacks = []
        self.apply_attempts = 0
        self.block_started = threading.Event()
        self.block_release = None

    def prepare(self, node, magic, peers):
        self.prepared.append((node, magic, peers))

    def status(self, node, magic):
        return {"peers": [{"peer": peer, "point": points[0]} for peer, points in self.saved.items() if points]}

    def intersection_points(self, node, magic, peer):
        return deepcopy(self.saved.get(peer, []))

    def intersect(self, node, magic, peer, point):
        self.intersections.append((peer, point))
        self.saved[peer] = [deepcopy(point)]

    def apply(self, node, magic, peer, header, raw):
        self.apply_attempts += 1
        self.order.append(("apply", peer, raw))
        self.block_started.set()
        if self.block_release is not None:
            assert self.block_release.wait(3), "test did not release in-flight transaction"
        if self.fail_apply is not None:
            error, self.fail_apply = self.fail_apply, None
            raise error
        self.applied.append((peer, deepcopy(header), raw))
        self.saved[peer] = [{"slot": header["slot"], "hash": header["block_hash"]}]
        return {"event_ref": "syn://event/" + header["block_hash"]}

    def rollback(self, node, magic, peer, point):
        self.rollbacks.append((peer, deepcopy(point)))
        self.saved[peer] = [deepcopy(point)]


class Client:
    def __init__(self, updates=(), *, intersect=None, order=None, peer=PEER):
        self.updates = asyncio.Queue()
        for update in updates:
            self.updates.put_nowait(update)
        self.intersect = intersect
        self.order = order if order is not None else []
        self.peer = peer
        self.tip_calls = 0
        self.find_calls = []
        self.fetches = []
        self.waiting = asyncio.Event()
        self.closed = False

    async def intersect_tip(self):
        self.tip_calls += 1
        return Intersect(BASE, Tip(BASE, 1))

    async def find_intersect(self, points):
        self.find_calls.append(points)
        return self.intersect if self.intersect is not None else Intersect(points[0], TIP)

    async def next_update(self):
        self.waiting.set()
        update = await self.updates.get()
        if isinstance(update, Exception):
            raise update
        return update

    async def fetch_block(self, header):
        self.order.append(("fetch", self.peer, header.block_hash))
        self.fetches.append(header)
        return b"complete-block"

    async def close(self):
        self.closed = True


class Connections:
    def __init__(self, peers):
        self.peers = {key: list(value) for key, value in peers.items()}
        self.calls = []

    async def __call__(self, host, port, magic, **kwargs):
        peer = f"{host}:{port}"
        self.calls.append((peer, magic, kwargs))
        if not self.peers[peer]:
            raise CardanoProtocolError("Fixture has no more connections")
        return self.peers[peer].pop(0)


def test_two_trusted_peers_follow_independently_and_fetch_before_recording():
    async def run():
        order = []
        first = Client([RollForward(HEAD, TIP), RollBackward(BASE, TIP)], order=order)
        second = Client([RollForward(HEAD, TIP)], order=order, peer=SECOND)
        store = Store(order=order)
        connections = Connections({PEER: [first], SECOND: [second]})
        runtime = CardanoRuntime(None, NODE, config((PEER, SECOND)), store=store, connect=connections)
        await runtime.start()
        await runtime.start()  # idempotent start cannot double the consumers
        try:
            await eventually(lambda: len(store.applied) == 2 and len(store.rollbacks) == 1)
            assert len(connections.calls) == 2
            assert first.tip_calls == second.tip_calls == 1
            assert not first.find_calls and not second.find_calls
            assert {peer for peer, _, _ in store.applied} == {PEER, SECOND}
            assert all(header == asdict(HEAD) and raw == b"complete-block" for _, header, raw in store.applied)
            for peer in (PEER, SECOND):
                assert next(i for i, call in enumerate(order) if call[:2] == ("fetch", peer)) < next(
                    i for i, call in enumerate(order) if call[:2] == ("apply", peer))
            assert store.rollbacks == [(PEER, {"slot": BASE.slot, "hash": BASE.hash})]
            status = runtime.status()
            assert status["running"] and status["mode"] == "trusted-peer-observations"
            assert sum(p["blocks_received"] for p in status["peers"]) == 2
            status["peers"][0]["attempts"] = 999
            assert runtime.status()["peers"][0]["attempts"] == 1
        finally:
            await runtime.stop()
        assert first.closed and second.closed
        assert not runtime.status()["running"]
    asyncio.run(run())


@pytest.mark.parametrize("saved", [[None], [{"slot": BASE.slot, "hash": BASE.hash}, None]])
def test_persisted_intersections_including_origin_never_reset_to_tip(saved):
    async def run():
        client = Client()
        store = Store(saved={PEER: saved})
        runtime = CardanoRuntime(None, NODE, config(), store=store,
                                connect=Connections({PEER: [client]}))
        await runtime.start()
        try:
            await asyncio.wait_for(client.waiting.wait(), 1)
            expected = [Point() if item is None else Point(item["slot"], item["hash"]) for item in saved]
            assert client.find_calls == [expected]
            assert client.tip_calls == 0
            assert store.intersections == [(PEER, saved[0])]
        finally:
            await runtime.stop()
    asyncio.run(run())


def test_explicit_start_point_is_used_on_a_fresh_source():
    async def run():
        client, store = Client(), Store()
        runtime = CardanoRuntime(None, NODE, config(startSlot=BASE.slot, startHash=BASE.hash),
                                store=store, connect=Connections({PEER: [client]}))
        await runtime.start()
        try:
            await asyncio.wait_for(client.waiting.wait(), 1)
            assert client.find_calls == [[BASE]] and client.tip_calls == 0
            assert store.intersections == [(PEER, {"slot": BASE.slot, "hash": BASE.hash})]
        finally:
            await runtime.stop()
    asyncio.run(run())


def test_database_failure_reconnects_from_last_committed_point():
    async def run():
        first = Client([RollForward(HEAD, TIP)])
        second = Client([RollForward(HEAD, TIP)])
        store = Store(fail_apply=SQLAlchemyError("temporary database failure"))
        runtime = CardanoRuntime(None, NODE, config(), store=store,
                                connect=Connections({PEER: [first, second]}))
        await runtime.start()
        try:
            await eventually(lambda: len(store.applied) == 1)
            assert first.closed and first.tip_calls == 1
            assert second.tip_calls == 0 and second.find_calls == [[BASE]]
            assert store.apply_attempts == 2
            assert len(store.applied) == 1
            assert store.saved[PEER] == [{"slot": HEAD.slot, "hash": HEAD.block_hash}]
            assert runtime.status()["peers"][0]["blocks_received"] == 1
        finally:
            await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("intersection", [None, Point(99, "f" * 64)])
def test_missing_or_unoffered_intersection_preserves_durable_checkpoint(intersection):
    async def run():
        saved = [{"slot": BASE.slot, "hash": BASE.hash}]
        client = Client(intersect=Intersect(intersection, TIP))
        store = Store(saved={PEER: saved})
        runtime = CardanoRuntime(None, NODE, config(), store=store,
                                connect=Connections({PEER: [client]}))
        await runtime.start()
        try:
            await eventually(lambda: client.closed)
            assert store.saved[PEER] == saved
            assert not store.intersections and not store.applied
            assert client.tip_calls == 0
            assert runtime.status()["peers"][0]["state"] == "retrying"
        finally:
            await runtime.stop()
    asyncio.run(run())


def test_stop_joins_in_flight_database_transaction_before_closing_source():
    async def run():
        client = Client([RollForward(HEAD, TIP)])
        store = Store()
        store.block_release = threading.Event()
        runtime = CardanoRuntime(None, NODE, config(), store=store,
                                connect=Connections({PEER: [client]}))
        await runtime.start()
        stopping = None
        try:
            await eventually(store.block_started.is_set)
            stopping = asyncio.create_task(runtime.stop())
            await asyncio.sleep(.03)
            assert not stopping.done() and not client.closed
            assert not store.applied
            store.block_release.set()
            await asyncio.wait_for(stopping, 1)
            assert len(store.applied) == 1 and client.closed
            assert not runtime.status()["running"]
            committed = deepcopy(store.applied)
            await asyncio.sleep(.02)
            assert store.applied == committed
        finally:
            store.block_release.set()
            if stopping is not None:
                await stopping
            await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("error,store_failure", [
    (UnsupportedEra("future era"), False),
    (CardanoStoreConflict("object identity collision"), True),
])
def test_unsupported_era_and_semantic_conflict_block_without_retry(error, store_failure):
    async def run():
        client = Client([RollForward(HEAD, TIP)] if store_failure else [error])
        store = Store(fail_apply=error if store_failure else None)
        connections = Connections({PEER: [client]})
        runtime = CardanoRuntime(None, NODE, config(), store=store, connect=connections)
        await runtime.start()
        try:
            await eventually(lambda: client.closed)
            await asyncio.sleep(.12)
            assert runtime.status()["peers"][0]["state"] == "blocked"
            assert len(connections.calls) == 1
            assert not store.applied
            runtime.check()  # A blocked peer is reported separately from a runtime bug.
        finally:
            await runtime.stop()
    asyncio.run(run())


def test_disabled_service_never_prepares_or_connects():
    async def run():
        store = Store()
        connections = Connections({PEER: []})
        runtime = CardanoRuntime(None, None, CardanoConfig(peers=(PEER,)),
                                store=store, connect=connections)
        await runtime.start()
        await runtime.stop()
        assert not store.prepared and not connections.calls
        assert runtime.status()["peers"][0]["state"] == "disabled"
        assert not runtime.status()["running"]
    asyncio.run(run())
