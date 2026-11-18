"""Group discovery retains a fixed native cohort and bounded relay lifecycle."""
from __future__ import annotations

import copy
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import PeerInfo
from multiaddr import Multiaddr
import pytest
import trio
from trio.testing import MockClock

from syn.agent.pubsub import NativePubSub
from syn import mesh_gossipsub as gossip
from syn_client.operator_protocol import SynProtocolError, canonical
from syn_client.pubsub import RELAY_BINDING_DOMAIN, group_hash, relay_proof_body

from test_pubsub import fixture  # noqa: F401 - shared explicit synthetic custody


@pytest.fixture
def tmp_path():
    # Exercise the real private-file checks. The shared workspace may have
    # writable ancestors, while root-owned sticky /tmp is explicitly allowed.
    with TemporaryDirectory(prefix="syn-gossip-config-", dir="/tmp") as directory:
        yield Path(directory).resolve()


def configuration(tmp_path, fixture, **changes):
    servers, selected = fixture
    directory = tmp_path / "relay"
    directory.mkdir(mode=0o700, exist_ok=True)
    identity = directory / "seed"
    identity.write_bytes(b"r" * 32)
    identity.chmod(0o600)
    return {"profile": gossip.PROFILE, "scope": selected,
            "identity_path": str(identity), "listen": "/ip4/127.0.0.1/tcp/0",
            "endpoints": [{"node": servers[0].expected_hash,
                           "socket": "/synthetic/agent.sock", "owner_uid": 1000}],
            **changes}


def local_hello(server, selected):
    node = NativePubSub(server)
    node.selected = selected
    return node.hello()


def proof(server, selected, keys):
    hello = local_hello(server, selected)
    body = relay_proof_body(hello, selected, keys.public_key.to_bytes())
    return {"hello": hello, "binding": {"body": body,
        "signature": server.signer.sign(RELAY_BINDING_DOMAIN + canonical(body)).hex()}}


def authentication(relay, proofs):
    return {"profile": gossip.AUTH_PROFILE, "group_hash": relay.group_hash, "proofs": proofs}


def test_dynamic_profile_uses_group_providers_without_preselected_relays(tmp_path, fixture):
    relay = gossip.GossipRelay(configuration(tmp_path, fixture))
    servers, selected = fixture
    assert relay.dynamic
    assert relay.peers == {} and relay.dial == []
    assert relay.local_nodes == {servers[0].expected_hash}
    assert relay.remote_nodes == {servers[1].expected_hash}
    assert relay.provider_key == b"\x12\x20" + bytes.fromhex(group_hash(selected))
    assert relay.topic == "/syn/private-pubsub/1/" + group_hash(selected)
    assert relay.discovery_config.bootstrapPeers == gossip.IPFS_BOOTSTRAP_PEERS
    assert relay.discovery_config.allowPrivateAddresses is False
    reversed_scope = {**selected, "members": list(reversed(selected["members"]))}
    reordered = gossip.GossipRelay(configuration(tmp_path, fixture, scope=reversed_scope))
    assert reordered.provider_key == relay.provider_key
    assert reordered.topic == relay.topic


def test_legacy_profile_preserves_explicit_peer_assignment(tmp_path, fixture):
    servers, _ = fixture
    remote = ID.from_pubkey(create_new_key_pair().public_key)
    config = configuration(tmp_path, fixture, profile=gossip.LEGACY_PROFILE,
        peers=[{"peer_id": str(remote), "address": "/ip4/127.0.0.1/tcp/4001",
                "nodes": [servers[1].expected_hash]}])
    relay = gossip.GossipRelay(config)
    assert relay.dynamic is False and relay.discovery_config is None
    assert relay.peers == {remote: frozenset({servers[1].expected_hash})}
    assert relay.dial[0].peer_id == remote


@pytest.mark.parametrize("change", [
    {"profile": "syn.private-gossipsub-config/99"}, {"peers": []},
    {"discovery": {"unrecognized": True}}, {"discovery": []},
    {"discovery": {"bootstrapPeers": "not-an-array"}},
    {"discovery": {"announceAddresses": "not-an-array"}},
    {"discovery": {"discoveryIntervalSeconds": True}},
    {"discovery": {"discoveryIntervalSeconds": 0}},
    {"discovery": {"lookupTimeoutSeconds": float("inf")}},
    {"discovery": {"allowPrivateAddresses": "yes"}},
    {"discovery": {"allowPrivateAddresses": True}},
    {"listen": "/ip4/127.0.0.1/tcp/04001"},
    {"listen": "/dns4/example.test/tcp/4001"},
])
def test_invalid_discovery_config_is_rejected(tmp_path, fixture, change):
    with pytest.raises(SynProtocolError):
        gossip.GossipRelay(configuration(tmp_path, fixture, **change))


@pytest.mark.parametrize("address", [
    "/ip4/0.0.0.0/tcp/4001", "/ip4/224.0.0.1/tcp/4001",
    "/ip4/169.254.1.1/tcp/4001", "/ip4/127.0.0.1/tcp/4001",
    "/ip4/10.0.0.1/tcp/4001", "/ip4/8.8.8.8/tcp/0",
    "/ip4/8.8.8.8/tcp/04001", "/ip6/2001:4860:4860:0:0:0:0:8888/tcp/4001",
    "/dns4/example.test/tcp/4001",
])
def test_public_discovery_refuses_unsafe_or_noncanonical_announcements(tmp_path, fixture, address):
    with pytest.raises(SynProtocolError):
        gossip.GossipRelay(configuration(tmp_path, fixture,
            discovery={"announceAddresses": [address]}))


def test_explicit_lab_discovery_preserves_empty_bootstrap_override(tmp_path, fixture):
    relay = gossip.GossipRelay(configuration(tmp_path, fixture, discovery={
        "bootstrapPeers": [], "allowPrivateAddresses": True,
        "announceAddresses": ["/ip4/127.0.0.1/tcp/4001"]}))
    assert relay.discovery_config.bootstrapPeers == ()
    assert relay.discovery_config.announceAddresses == ("/ip4/127.0.0.1/tcp/4001",)


@pytest.mark.parametrize("node", [[], {}, None, True, "bad", "AA" * 32])
def test_malformed_endpoint_nodes_fail_as_protocol_errors(tmp_path, fixture, node):
    with pytest.raises(SynProtocolError):
        gossip.GossipRelay(configuration(tmp_path, fixture, endpoints=[
            {"node": node, "socket": "/synthetic/agent.sock", "owner_uid": 1000}]))


@pytest.mark.parametrize("invalid", ["duplicate", "local", "signature"])
def test_entire_authentication_claim_is_checked_before_assignment(tmp_path, fixture, invalid):
    relay = gossip.GossipRelay(configuration(tmp_path, fixture))
    servers, selected = fixture
    keys = create_new_key_pair()
    peer = ID.from_pubkey(keys.public_key)
    remote = proof(servers[1], selected, keys)
    extra = copy.deepcopy(remote) if invalid != "local" else proof(servers[0], selected, keys)
    if invalid == "signature":
        extra["binding"]["signature"] = "00" * 64
    packet = authentication(relay, [remote, extra])
    before = copy.deepcopy(relay.hellos)
    with pytest.raises(SynProtocolError):
        relay._accept_proofs(peer, packet)
    assert relay.peers == {} and relay.hellos == before


def test_duplicate_claim_across_relays_does_not_replace_first_assignment(tmp_path, fixture):
    relay = gossip.GossipRelay(configuration(tmp_path, fixture))
    servers, selected = fixture
    first, second = create_new_key_pair(), create_new_key_pair()
    first_id, second_id = ID.from_pubkey(first.public_key), ID.from_pubkey(second.public_key)
    packet = authentication(relay, [proof(servers[1], selected, first)])
    relay._accept_proofs(first_id, packet)
    before = copy.deepcopy(relay.hellos)
    with pytest.raises(SynProtocolError, match="more than one"):
        relay._accept_proofs(second_id, authentication(relay, [proof(servers[1], selected, second)]))
    assert relay.peers == {first_id: frozenset({servers[1].expected_hash})}
    assert relay.hellos == before
    relay._accept_proofs(first_id, packet)  # Identical retries are idempotent.
    assert relay.hellos == before


def test_provider_replay_cannot_claim_another_authenticated_peer(tmp_path, fixture):
    relay = gossip.GossipRelay(configuration(tmp_path, fixture))
    servers, selected = fixture
    original, attacker = create_new_key_pair(), create_new_key_pair()
    packet = authentication(relay, [proof(servers[1], selected, original)])
    with pytest.raises(SynProtocolError, match="authenticated transport"):
        relay._accept_proofs(ID.from_pubkey(attacker.public_key), packet)
    assert relay.peers == {} and relay.hellos == {}


def test_active_session_cannot_gain_a_new_relay_assignment(tmp_path, fixture):
    relay = gossip.GossipRelay(configuration(tmp_path, fixture))
    servers, selected = fixture
    keys = create_new_key_pair()
    relay.active = True
    with pytest.raises(SynProtocolError, match="live session"):
        relay._accept_proofs(ID.from_pubkey(keys.public_key),
            authentication(relay, [proof(servers[1], selected, keys)]))
    assert relay.peers == {}


class QuietDHT:
    def __init__(self, *args, **kwargs):
        pass

    async def bootstrap(self):
        return 0

    def status(self):
        return {"routingPeers": 0}

    async def find_providers(self, key):
        return []


def lab_config(tmp_path, fixture):
    return configuration(tmp_path, fixture, discovery={"bootstrapPeers": [],
        "allowPrivateAddresses": True, "lookupTimeoutSeconds": 30,
        "requestTimeoutSeconds": 1, "discoveryIntervalSeconds": 1})


def test_stop_cancels_pre_attach_bootstrap_promptly(tmp_path, fixture, monkeypatch):
    async def exercise():
        entered, cancelled, finished = trio.Event(), trio.Event(), trio.Event()
        class SlowBootstrap(QuietDHT):
            async def bootstrap(self):
                entered.set()
                try:
                    await trio.sleep_forever()
                finally:
                    cancelled.set()
        monkeypatch.setattr(gossip, "AminoDHTClient", SlowBootstrap)
        relay = gossip.GossipRelay(lab_config(tmp_path, fixture))
        relay._attach_local = lambda: pytest.fail("Stopping bootstrap must not attach native Agents")
        async def run():
            try:
                await relay.run(lambda: pytest.fail("No cohort became ready"))
            finally:
                finished.set()
        async with trio.open_nursery() as nursery:
            nursery.start_soon(run)
            with trio.fail_after(3):
                await entered.wait()
            relay.stopped.set()
            with trio.fail_after(1):
                await finished.wait()
            assert cancelled.is_set()
            assert not relay.fault.is_set()
            assert relay.discovery_status()["state"] == "stopped"
    trio.run(exercise)


def test_native_failure_concurrent_with_stop_still_fences_runtime(tmp_path, fixture, monkeypatch):
    async def exercise():
        monkeypatch.setattr(gossip, "AminoDHTClient", QuietDHT)
        relay = gossip.GossipRelay(lab_config(tmp_path, fixture))
        servers, _ = fixture
        child, parent = socket.socketpair()
        child.settimeout(1)
        def failed_attach():
            # A real native error must survive a simultaneous controller stop;
            # only cancellation itself is a normal shutdown condition.
            relay.connections[servers[0].expected_hash] = parent
            relay.stopped.set()
            raise SynProtocolError("Synthetic native custody failure during stop")
        relay._attach_local = failed_attach
        try:
            with pytest.raises(BaseExceptionGroup) as failure:
                with trio.fail_after(3):
                    await relay.run(lambda: pytest.fail("Failed native custody cannot become ready"))
            assert failure.value.subgroup(SynProtocolError) is not None
            assert relay.fault.is_set() and relay.stopped.is_set()
            assert relay.discovery_status()["state"] == "stopped"
            assert parent.fileno() == -1
            assert child.recv(1) == b""
        finally:
            child.close()
            parent.close()
    trio.run(exercise)


@pytest.mark.parametrize("external_cancellation", [False, True])
def test_inflight_discovery_cancels_and_native_socket_closes(tmp_path, fixture, monkeypatch, external_cancellation):
    async def exercise():
        entered, cancelled, finished = trio.Event(), trio.Event(), trio.Event()
        class SlowLookup(QuietDHT):
            async def find_providers(self, key):
                entered.set()
                try:
                    await trio.sleep_forever()
                finally:
                    cancelled.set()
        monkeypatch.setattr(gossip, "AminoDHTClient", SlowLookup)
        relay = gossip.GossipRelay(lab_config(tmp_path, fixture))
        servers, selected = fixture
        child, parent = socket.socketpair()
        child.settimeout(1)
        def attach():
            relay.connections[servers[0].expected_hash] = parent
            relay.hellos[servers[0].expected_hash] = local_hello(servers[0], selected)
        relay._attach_local = attach
        async def run():
            try:
                await relay.run(lambda: pytest.fail("Incomplete cohort cannot be ready"))
            finally:
                finished.set()
        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(run)
                with trio.fail_after(3):
                    await entered.wait()
                if external_cancellation:
                    nursery.cancel_scope.cancel()
                else:
                    relay.stopped.set()
                    with trio.fail_after(1):
                        await finished.wait()
            assert cancelled.is_set() and finished.is_set()
            assert child.recv(1) == b""
            assert parent.fileno() == -1
            assert relay.stopped.is_set()
            assert relay.fault.is_set() is external_cancellation
        finally:
            child.close()
            parent.close()
    trio.run(exercise)


def test_missing_cohort_deadline_fences_runtime(tmp_path, fixture, monkeypatch):
    async def exercise():
        monkeypatch.setattr(gossip, "AminoDHTClient", QuietDHT)
        monkeypatch.setattr(gossip, "STARTUP_SECONDS", 0.05)
        relay = gossip.GossipRelay(lab_config(tmp_path, fixture))
        servers, selected = fixture
        relay._attach_local = lambda: relay.hellos.update(
            {servers[0].expected_hash: local_hello(servers[0], selected)})
        with pytest.raises(BaseExceptionGroup) as failure:
            with trio.fail_after(2):
                await relay.run(lambda: pytest.fail("Incomplete cohort cannot be ready"))
        def leaves(error):
            if isinstance(error, BaseExceptionGroup):
                return [leaf for child in error.exceptions for leaf in leaves(child)]
            return [error]
        assert any(isinstance(error, SynProtocolError) and "roster did not become ready" in str(error)
                   for error in leaves(failure.value))
        assert relay.fault.is_set() and relay.stopped.is_set()
        assert not relay.active
    trio.run(exercise)


def test_dht_outage_preserves_an_already_authenticated_mesh(tmp_path, fixture):
    async def exercise():
        relay = gossip.GossipRelay(lab_config(tmp_path, fixture))
        servers, selected = fixture
        keys = create_new_key_pair()
        peer = ID.from_pubkey(keys.public_key)
        relay.hellos[servers[0].expected_hash] = local_hello(servers[0], selected)
        relay._accept_proofs(peer, authentication(relay, [proof(servers[1], selected, keys)]))
        relay.active = True
        attempts = []
        class UnavailableDHT(QuietDHT):
            async def bootstrap(self):
                attempts.append(True)
                raise OSError("Synthetic discovery outage")
            async def find_providers(self, key):
                pytest.fail("Active cohort must not be silently rediscovered")
        async def publish(*_):
            await trio.lowlevel.checkpoint()
        async def get():
            await trio.sleep_forever()
        relay.dht = UnavailableDHT()
        relay.host = SimpleNamespace(get_addrs=lambda: [])
        relay.pubsub = SimpleNamespace(peer_topics={relay.topic: {peer}}, publish=publish)
        before = copy.deepcopy(relay.hellos)
        async with trio.open_nursery() as nursery:
            nursery.start_soon(relay._discover)
            nursery.start_soon(relay._serve, relay.host, SimpleNamespace(get=get),
                               lambda: pytest.fail("Ready cohort must not restart"))
            await trio.sleep(2)
            assert attempts
            assert relay.active and not relay.fault.is_set()
            assert relay.hellos == before
            assert relay.peers == {peer: frozenset({servers[1].expected_hash})}
            relay.stopped.set()
            nursery.cancel_scope.cancel()
    trio.run(exercise, clock=MockClock(autojump_threshold=0))


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_failed_auth_stream_close_does_not_leak_capacity(tmp_path, fixture, monkeypatch, direction):
    async def exercise():
        relay = gossip.GossipRelay(lab_config(tmp_path, fixture))
        peer = ID.from_pubkey(create_new_key_pair().public_key)
        class BrokenClose:
            muxed_conn = SimpleNamespace(peer_id=peer)
            async def write(self, raw):
                pass
            async def close(self):
                raise OSError("Synthetic failed close")
        stream = BrokenClose()
        async def invalid_frame(*_):
            return canonical({"profile": "invalid"})
        async def connect(*_):
            pass
        async def new_stream(*_):
            return stream
        monkeypatch.setattr(gossip, "read_frame", invalid_frame)
        monkeypatch.setattr(gossip, "seed_dial_addresses", lambda host, info, **_: info)
        relay.host = SimpleNamespace(connect=connect, new_stream=new_stream)
        relay._auth_packet = lambda: {"profile": gossip.AUTH_PROFILE, "group_hash": relay.group_hash, "proofs": []}
        before = relay._auth_slots.value
        if direction == "inbound":
            await relay._authenticate_stream(stream)
        else:
            await relay._authenticate_candidate(PeerInfo(peer, [Multiaddr("/ip4/127.0.0.1/tcp/4001")]))
        assert relay._auth_slots.value == before
        assert relay._auth_pending == set()
        assert relay.peers == {}
        assert relay.discovery_status()["rejectedCandidates"] == 1
    trio.run(exercise)
