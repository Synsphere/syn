"""Real local hosts keep Operator publication inside their live transport run."""
from __future__ import annotations

import asyncio
import hashlib
from threading import Event
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import PeerInfo
import multiaddr
import pytest
import trio

from syn import peer_record
from syn.config import P2PConfig
from syn.p2p import Libp2pNode


def config(**changes):
    values = dict(listenAddresses=("/ip4/127.0.0.1/tcp/0",), bootstrapPeers=(),
        mdnsEnabled=False, upnpEnabled=False, aminoEnabled=False,
        allowPrivateAddresses=True, startupTimeoutSeconds=5)
    return P2PConfig(**{**values, **changes})


class Signer:
    def __init__(self):
        self.key = Ed25519PrivateKey.from_private_bytes(b"l" * 32)
        self.public_key = self.key.public_key().public_bytes_raw()

    def sign(self, payload):
        return self.key.sign(payload)


def operator(signer):
    # Avoid accessing potentially blocking signer.public_key during setup.
    public = Ed25519PrivateKey.from_private_bytes(b"l" * 32).public_key().public_bytes_raw()
    return SimpleNamespace(signer=signer, expected_hash=hashlib.sha256(public).hexdigest(),
                           config={"network_id": "syn"})


async def until(predicate, *, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("mode", ["no-operator", "disabled"])
def test_unbound_or_disabled_publication_never_reads_an_operator_key(mode):
    class ForbiddenSigner:
        @property
        def public_key(self):
            pytest.fail("No publication must mean no custody access")
    async def exercise():
        node = Libp2pNode(config(publishOperatorRecord=mode != "disabled"),
                          None if mode == "no-operator" else operator(ForbiddenSigner()))
        try:
            await node.start()
            assert node.status()["running"]
            assert node.status()["publication"]["state"] == mode
            assert node.public_record() is None
        finally:
            await node.stop()
    asyncio.run(exercise())


def test_pending_native_approval_does_not_block_ready_or_stop_or_publish_late():
    entered, release, returned = Event(), Event(), Event()
    signer = Signer()
    def sign_peer_record(body):
        entered.set()
        try:
            if not release.wait(10):
                raise RuntimeError("Test approval was not released")
            return signer.sign(peer_record.DOMAIN + peer_record.canonical(body))
        finally:
            returned.set()
    signer.sign_peer_record = sign_peer_record
    async def exercise():
        node = Libp2pNode(config(), operator(signer))
        try:
            await node.start()
            assert await asyncio.to_thread(entered.wait, 3)
            assert node.status()["running"]
            assert node.status()["publication"]["state"] == "signing"
            assert node.public_record() is None
            await asyncio.wait_for(node.stop(), timeout=2)
            assert not node.status()["running"]
            release.set()
            assert await asyncio.to_thread(returned.wait, 3)
            # The result belongs to a cancelled publication task, never to a
            # stopped host or a subsequent transport identity.
            await asyncio.sleep(0.05)
            assert node.public_record() is None
        finally:
            release.set()
            await node.stop()
    asyncio.run(exercise())


def test_native_publication_failure_stops_transport_and_fences_check():
    signer = Signer()
    def unavailable(body):
        raise RuntimeError("Native custody is unavailable")
    signer.sign_peer_record = unavailable
    async def exercise():
        node = Libp2pNode(config(), operator(signer))
        try:
            try:
                await node.start()
            except RuntimeError:
                pass  # Failure may race with the startup readiness event.
            await until(lambda: node._error is not None and not node.status()["running"])
            with pytest.raises(RuntimeError, match="lost readiness"):
                node.check()
            assert node.public_record() is None
        finally:
            await node.stop()
    asyncio.run(exercise())


def test_restart_rebinds_fresh_transport_to_the_same_operator():
    async def exercise():
        server = operator(Signer())
        node = Libp2pNode(config(), server)
        try:
            await node.start()
            await until(lambda: node.public_record() is not None)
            first = node.public_record()
            old_peer = node.status()["peerId"]
            await node.stop()
            assert node.public_record() is None
            await node.start()
            await until(lambda: node.public_record() is not None)
            second = node.public_record()
            new_peer = node.status()["peerId"]
            assert new_peer != old_peer
            assert first["body"]["operator"] == second["body"]["operator"] == server.expected_hash
            assert first["body"]["peer_id"] == old_peer
            assert second["body"]["peer_id"] == new_peer
            peer_record.verify(second, expected_operator=server.expected_hash,
                               expected_network="syn", expected_peer_id=new_peer)
            with pytest.raises(ValueError, match="authenticated transport peer"):
                peer_record.verify(first, expected_operator=server.expected_hash,
                                   expected_network="syn", expected_peer_id=new_peer)
        finally:
            await node.stop()
    asyncio.run(exercise())


def test_startup_timeout_cancels_an_initial_native_identity_read():
    entered, release = Event(), Event()
    public = Signer().public_key
    class SlowIdentity:
        @property
        def public_key(self):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Identity test read was not released")
            return public
    async def exercise():
        node = Libp2pNode(config(startupTimeoutSeconds=0.3), operator(SlowIdentity()))
        try:
            start = asyncio.create_task(node.start())
            assert await asyncio.to_thread(entered.wait, 2)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(start, timeout=2)
            assert not node.status()["running"]
            assert node._thread is None
            assert node.public_record() is None
        finally:
            release.set()
            await node.stop()
    asyncio.run(exercise())


@pytest.mark.parametrize("cancel_before_entry", [False, True])
def test_cancelled_asyncio_discovery_releases_trio_lookup_capacity(monkeypatch, cancel_before_entry):
    entered, cancelled = Event(), Event()
    worker_entered, release_worker, worker_done = Event(), Event(), Event()
    state = {"calls": 0}
    class SlowDHT:
        def __init__(self, *args, **kwargs):
            pass
        async def bootstrap(self):
            return 1
        def status(self):
            return {"lastLookupStatus": "ready"}
        async def find_providers(self, key, *, count):
            state["calls"] += 1
            if state["calls"] == 1:
                entered.set()
                try:
                    await trio.sleep_forever()
                finally:
                    cancelled.set()
            return []
    monkeypatch.setattr("syn.p2p.AminoDHTClient", SlowDHT)
    async def exercise():
        node = Libp2pNode(config(aminoEnabled=True))
        task = None
        original_run = trio.from_thread.run
        try:
            await node.start()
            if cancel_before_entry:
                def delayed_run(*args, **kwargs):
                    worker_entered.set()
                    try:
                        if not release_worker.wait(5):
                            raise RuntimeError("Discovery worker gate was not released")
                        return original_run(*args, **kwargs)
                    finally:
                        worker_done.set()
                monkeypatch.setattr(trio.from_thread, "run", delayed_run)
            task = asyncio.create_task(node.find_operator("1" * 64))
            assert await asyncio.to_thread((worker_entered if cancel_before_entry else entered).wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            if cancel_before_entry:
                # A Trio-thread barrier guarantees that its pending cancellation
                # callback has run before the delayed request enters the loop.
                await asyncio.to_thread(trio.from_thread.run_sync, lambda: None, trio_token=node._token)
                release_worker.set()
                assert await asyncio.to_thread(worker_done.wait, 2)
                monkeypatch.setattr(trio.from_thread, "run", original_run)
            await until(lambda: cancelled.is_set() and node._lookup_slots.value == 4, timeout=2)
            # Subsequent requests can use the released slot immediately; the
            # abandoned lookup did not linger until lookupTimeoutSeconds.
            assert await asyncio.wait_for(node.find_operator("1" * 64), timeout=2) == []
        finally:
            release_worker.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await node.stop()
    asyncio.run(exercise())


def test_wan_discovery_rejects_an_authentic_record_with_any_private_endpoint(monkeypatch):
    server = operator(Signer())
    transport = create_new_key_pair(b"m" * 32)
    remote_peer = ID.from_pubkey(transport.public_key)
    public_address = "/ip4/8.8.8.8/tcp/4001"
    private_address = "/ip4/10.0.0.1/tcp/4001"
    candidate = PeerInfo(remote_peer, [multiaddr.Multiaddr(public_address)])
    state = {"packet": peer_record.issue(server, transport.public_key.to_bytes(),
                                        [private_address, public_address]), "fetches": 0}
    class CandidateDHT:
        def __init__(self, *args, **kwargs):
            pass
        async def bootstrap(self):
            return 1
        def status(self):
            return {"lastLookupStatus": "ready"}
        async def find_providers(self, key, *, count):
            assert key == peer_record.provider_key(server.expected_hash)
            return [candidate]
    async def verified_fetch(host, peer, *, expected_operator, expected_network, timeout):
        assert peer.peer_id == remote_peer
        state["fetches"] += 1
        packet = state["packet"]
        peer_record.verify(packet, expected_operator=expected_operator,
            expected_network=expected_network, expected_peer_id=str(peer.peer_id))
        return packet
    monkeypatch.setattr("syn.p2p.AminoDHTClient", CandidateDHT)
    monkeypatch.setattr("syn.p2p.fetch_peer_record", verified_fetch)
    async def exercise():
        node = Libp2pNode(config(aminoEnabled=True, allowPrivateAddresses=False))
        try:
            await node.start()
            assert await node.find_operator(server.expected_hash) == []
            assert state["fetches"] == 1
            # A control record from the same authenticated key, with only an
            # allowed endpoint, is returned intact with its original signature.
            state["packet"] = peer_record.issue(server, transport.public_key.to_bytes(), [public_address])
            found = await node.find_operator(server.expected_hash)
            assert found == [state["packet"]]
            assert state["fetches"] == 2
        finally:
            await node.stop()
    asyncio.run(exercise())
