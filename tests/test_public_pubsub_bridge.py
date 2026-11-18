"""Cancellation and capacity on the actual asyncio-to-Trio public control bridge."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import trio

from syn.config import P2PConfig
from syn.p2p import Libp2pNode
from syn.public_pubsub import PublicPubsubBusy, PublicPubsubUnavailable


URL = "syn://event/bridge-example"


def node_for_test(*, request_timeout=5):
    return Libp2pNode(P2PConfig(
        listenAddresses=("/ip4/127.0.0.1/tcp/0",), bootstrapPeers=(),
        aminoEnabled=False, mdnsEnabled=False, upnpEnabled=False,
        publishOperatorRecord=False, allowPrivateAddresses=True,
        startupTimeoutSeconds=5, requestTimeoutSeconds=request_timeout,
    ))


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def stop(node):
    async with asyncio.timeout(3):
        await node.stop()


def test_cancelling_asyncio_caller_cancels_trio_control_and_restores_slot(monkeypatch):
    async def exercise():
        node = node_for_test()
        entered, cancelled = Event(), Event()
        pending = None
        await node.start()
        service, slots = node._public, node._public_slots
        original_join = service.join

        async def blocked_join(url, network_id):
            assert url == URL and network_id == "syn"
            entered.set()
            try:
                await trio.sleep_forever()
            finally:
                cancelled.set()

        monkeypatch.setattr(service, "join", blocked_join)
        try:
            pending = asyncio.create_task(node.join_public_group(URL))
            await eventually(entered.is_set)
            assert slots.value == 15
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await eventually(lambda: cancelled.is_set() and slots.value == 16)
            monkeypatch.setattr(service, "join", original_join)
            joined = await node.join_public_group(URL)
            assert joined["joined"] and joined["url"] == URL
            assert joined["network_id"] == "syn"
            node.check()
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stop(node)
        assert not node.status()["running"]
        assert node._error is None
    asyncio.run(exercise())


def test_control_capacity_is_bounded_and_stop_cancels_all_inflight_calls(monkeypatch):
    async def exercise():
        # Exercise the bridge's 16-call admission limit independently of the
        # machine's CPU-dependent default asyncio worker-pool size.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=24))
        node = node_for_test()
        all_entered = Event()
        active, cancelled = set(), set()
        pending = []
        await node.start()
        service, slots = node._public, node._public_slots

        async def blocked_join(url, network_id):
            assert network_id == "syn"
            active.add(url)
            if len(active) == 16:
                all_entered.set()
            try:
                await trio.sleep_forever()
            finally:
                active.remove(url)
                cancelled.add(url)

        monkeypatch.setattr(service, "join", blocked_join)
        try:
            pending = [asyncio.create_task(node.join_public_group(f"syn://event/wait-{index}"))
                       for index in range(16)]
            await eventually(all_entered.is_set)
            assert slots.value == 0
            with pytest.raises(PublicPubsubBusy, match="capacity"):
                await node.join_public_group("syn://event/overflow")
            assert len(active) == 16
            assert "syn://event/overflow" not in active
            await stop(node)
            async with asyncio.timeout(2):
                results = await asyncio.gather(*pending, return_exceptions=True)
            assert all(isinstance(result, PublicPubsubUnavailable) for result in results), results
            assert not active and len(cancelled) == 16
            assert slots.value == 16
            assert node._error is None
            assert not node.status()["running"]
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await stop(node)
    asyncio.run(exercise())


def test_request_timeout_releases_slot_and_allows_subsequent_control(monkeypatch):
    async def exercise():
        node = node_for_test(request_timeout=0.15)
        entered, cancelled = Event(), Event()
        await node.start()
        service, slots = node._public, node._public_slots
        original_join = service.join

        async def blocked_join(*_):
            entered.set()
            try:
                await trio.sleep_forever()
            finally:
                cancelled.set()

        monkeypatch.setattr(service, "join", blocked_join)
        try:
            async with asyncio.timeout(2):
                with pytest.raises(PublicPubsubUnavailable, match="timed out"):
                    await node.join_public_group(URL)
            assert entered.is_set() and cancelled.is_set()
            assert slots.value == 16
            monkeypatch.setattr(service, "join", original_join)
            result = await node.join_public_group(URL)
            assert result["joined"] and result["url"] == URL
            node.check()
        finally:
            await stop(node)
        assert node._error is None
    asyncio.run(exercise())
