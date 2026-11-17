"""Peer checkpoint exchange using real signatures, ledger transactions and loopback HTTP.

SQLite covers transaction logic, not PostgreSQL locks/triggers or Windows DPAPI.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from syn.communication import metadata as ledger_metadata, messages
from syn.operator import dag, dag_proofs, dag_evidence
from syn.witnessing.config import Peer, WitnessConfig
from syn.witnessing.anchors import reconcile
from syn.witnessing.protocol import classify, strict_json, verify_equivocation
from syn.witnessing.runtime import poll_peer
from syn.witnessing.store import WitnessStore, metadata, heads, records
from syn.witnessing.transport import PeerClient, PeerUnavailable


class Source:
    def __init__(self, network="syn"):
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.pin = hashlib.sha256(self.public).hexdigest()
        self.network = network
        self.leaves, self.checkpoints = [], {}

    def grow(self, count):
        old = self.checkpoints[max(self.checkpoints)] if self.checkpoints else None
        self.leaves += [hashlib.sha256(str(i).encode()).hexdigest() for i in range(len(self.leaves), count)]
        body = dag._checkpoint_body(self.pin, self.network, count, dag_proofs.root(self.leaves),
            dag_evidence.checkpoint_digest(old) if old else None)
        cp = {"body": body, "public_key": self.public.hex(),
              "signature": self.key.sign(dag.CHECKPOINT_DOMAIN + dag.canonical(body)).hex()}
        self.checkpoints[count] = cp
        return cp

    def checkpoint(self):
        return deepcopy(self.checkpoints[max(self.checkpoints)])

    def consistency(self, old, new):
        return {"profile": "syn.operator-dag-consistency/1", "older": deepcopy(self.checkpoints[old]),
                "newer": deepcopy(self.checkpoints[new]), "audit_path": dag_proofs.consistency(self.leaves[:new], old)}

    def fork(self, count):
        other = Source(self.network)
        other.key, other.public, other.pin = self.key, self.public, self.pin
        other.leaves = [hashlib.sha256(("fork-" + str(i)).encode()).hexdigest() for i in range(count)]
        other.grow(count)
        return other


class Anchor:
    def __init__(self, peer):
        self.peer, self.value, self.fail = peer, None, False

    def read(self):
        return deepcopy(self.value)

    def advance(self, packet, path):
        if self.fail:
            raise OSError("simulated custody failure")
        bundle = None if self.value is None else {"profile": "syn.operator-dag-consistency/1",
            "older": self.value, "newer": packet, "audit_path": path}
        assert classify(self.value, packet, self.peer, bundle) in {"accepted", "unchanged"}
        self.value = deepcopy(packet)


@pytest.fixture
def setup(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "witness.db"))
    metadata.create_all(engine)
    ledger_metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    observer, source = Source(), Source()
    peer = Peer(source.pin, "syn", "https://peer.example")
    store = WitnessStore(sessions, observer.pin, "syn://thing/local-witness")
    anchor = Anchor(peer)
    yield store, source, peer, anchor, sessions
    engine.dispose()


def test_accept_extension_idempotence_and_common_transaction_ledger(setup):
    store, source, peer, anchor, sessions = setup
    source.grow(3)
    assert poll_peer(store, peer, anchor, source)["status"] == "accepted"
    assert poll_peer(store, peer, anchor, source) == {"status": "unchanged", "recorded": False}
    source.grow(9)
    assert poll_peer(store, peer, anchor, source)["status"] == "accepted"
    assert anchor.value == source.checkpoint()
    state = store.snapshot(peer)
    assert len(state["accepted"]) == 2
    with sessions() as s:
        receipts = s.execute(select(messages).order_by(messages.c.sequence)).mappings().all()
        entries = s.execute(select(records).order_by(records.c.sequence)).mappings().all()
    assert len(receipts) == len(entries) == 2
    from syn_client.protocol import verify_receipt
    for receipt, entry in zip(receipts, entries):
        verify_receipt({"schema_version": 1, **dict(receipt)}, store.workspace)
        assert entry["receiptHash"] == receipt["hash"]
        assert receipt["payload"]["digest"] == entry["digest"]
    assert receipts[1]["previous_hash"] == receipts[0]["hash"]


def test_stale_reply_never_rewinds_retained_checkpoint(setup):
    store, source, peer, anchor, _ = setup
    source.grow(2)
    old = source.checkpoint()
    source.grow(5)
    poll_peer(store, peer, anchor, source)
    source.checkpoints = {2: old}
    assert poll_peer(store, peer, anchor, source)["status"] == "stale"
    assert anchor.value["body"]["sequence"] == 5
    assert not store.snapshot(peer)["conflicted"]


def test_conflicting_signed_roots_are_retained_and_quarantined(setup):
    store, source, peer, anchor, _ = setup
    source.grow(5)
    poll_peer(store, peer, anchor, source)
    first = anchor.read()
    fork = source.fork(5)
    assert poll_peer(store, peer, anchor, fork)["status"] == "equivocation"
    assert anchor.read() == first
    last = store.snapshot(peer)["last"]
    assert verify_equivocation(last["previous_checkpoint"], last["checkpoint"], peer)
    source.grow(9)
    assert poll_peer(store, peer, anchor, source)["status"] == "quarantined"


def test_bad_extension_proof_is_not_falsely_called_equivocation(setup):
    store, source, peer, anchor, _ = setup
    source.grow(2)
    poll_peer(store, peer, anchor, source)
    source.grow(5)
    original = source.consistency
    source.consistency = lambda *args: {**original(*args), "audit_path": ["ff" * 32]}
    assert poll_peer(store, peer, anchor, source)["status"] == "invalid-proof"
    assert anchor.value["body"]["sequence"] == 2
    assert not store.snapshot(peer)["conflicted"]


def test_wrong_identity_and_signature_do_not_change_archive(setup):
    store, source, peer, anchor, _ = setup
    source.grow(1)
    poll_peer(store, peer, anchor, source)
    wrong = Source()
    wrong.grow(3)
    assert poll_peer(store, peer, anchor, wrong)["status"] == "invalid-checkpoint"
    source.grow(4)
    source.checkpoints[4]["signature"] = "00" * 64
    assert poll_peer(store, peer, anchor, source)["status"] == "invalid-checkpoint"
    assert anchor.value["body"]["sequence"] == 1


def test_remote_outage_is_a_receipted_nonfatal_transition(setup):
    store, source, peer, anchor, sessions = setup
    source.grow(2)
    poll_peer(store, peer, anchor, source)
    class Offline:
        def checkpoint(self):
            raise PeerUnavailable()
    assert poll_peer(store, peer, anchor, Offline())["recorded"] is True
    assert poll_peer(store, peer, anchor, Offline())["recorded"] is False
    assert poll_peer(store, peer, anchor, source)["status"] == "unchanged"
    assert store.snapshot(peer)["last"]["outcome"] == "unchanged"
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(records)) == 3


def test_host_write_failure_replays_committed_evidence_on_restart(setup):
    store, source, peer, anchor, _ = setup
    source.grow(1)
    anchor.fail = True
    with pytest.raises(OSError, match="custody"):
        poll_peer(store, peer, anchor, source)
    assert store.snapshot(peer)["checkpoint"] == source.checkpoint()
    anchor.fail = False
    assert poll_peer(store, peer, anchor, source)["status"] == "unchanged"
    assert anchor.value == source.checkpoint()


def test_database_rollback_is_detected_before_network_read(setup):
    store, source, peer, anchor, sessions = setup
    source.grow(2)
    poll_peer(store, peer, anchor, source)
    with sessions.begin() as s:
        s.execute(records.delete())  # SQLite intentionally permits fault injection.
        s.execute(heads.delete())
    class MustNotCall:
        def checkpoint(self):
            raise AssertionError("Network must not repair database rollback")
    with pytest.raises(ValueError, match="independently retained"):
        poll_peer(store, peer, anchor, MustNotCall())


def test_ledger_failure_rolls_back_head_and_public_evidence(setup, monkeypatch):
    store, source, peer, anchor, sessions = setup
    source.grow(1)
    def fail(*args):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(store.ledger, "appendInTransaction", fail)
    with pytest.raises(RuntimeError, match="ledger"):
        poll_peer(store, peer, anchor, source)
    assert store.snapshot(peer)["checkpoint"] is None and anchor.value is None
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(records)) == 0
        assert s.scalar(select(func.count()).select_from(heads)) == 0


def test_independent_initial_checkpoint_pin_is_enforced(setup):
    store, source, peer, _, _ = setup
    source.grow(2)
    pinned = Peer(peer.operator, peer.network, peer.url, initialCheckpoint="ab" * 32)
    anchor = Anchor(pinned)
    assert poll_peer(store, pinned, anchor, source)["status"] == "invalid-checkpoint"
    assert anchor.value is None


def test_invalid_newer_proof_cannot_substitute_retained_older_checkpoint(setup):
    store, source, peer, anchor, _ = setup
    source.grow(2)
    poll_peer(store, peer, anchor, source)
    source.grow(4)
    source.grow(6)
    source.consistency = lambda *_: {"profile": "syn.operator-dag-consistency/1",
        "older": source.checkpoints[4], "newer": source.checkpoints[6],
        "audit_path": dag_proofs.consistency(source.leaves, 4)}
    assert poll_peer(store, peer, anchor, source)["status"] == "invalid-proof"
    assert anchor.value["body"]["sequence"] == 2


@pytest.mark.parametrize("settings", [
    {"url": "http://127.0.0.1:8000"}, {"url": "https://user:pass@peer"},
    {"url": "file:///etc/passwd"}, {"url": "https://peer/path"},
    {"url": "https://peer/#fragment"}, {"url": "https://peer/?secret=x"},
    {"url": "https://peer:99999"}, {"operator": "AB" * 32},
    {"network": "wrong network"}, {"allowHttp": 1}, {"initialCheckpoint": "short"},
])
def test_peer_configuration_rejects_unsafe_or_unpinned_input(settings):
    with pytest.raises(ValueError):
        Peer(**{**{"operator": "ab" * 32, "network": "syn", "url": "https://peer"}, **settings})


@pytest.mark.parametrize("settings", [{"pollSeconds": 0}, {"timeoutSeconds": float("nan")},
    {"timeoutSeconds": True}, {"unknown": 1}])
def test_policy_validation(settings):
    with pytest.raises(ValueError):
        WitnessConfig.from_dict(settings)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'x' * 131073])
def test_network_json_is_bounded_and_rejects_ambiguous_fields(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


def test_archive_and_head_tampering_fail_verification(setup):
    store, source, peer, anchor, sessions = setup
    source.grow(2)
    poll_peer(store, peer, anchor, source)
    with sessions.begin() as s:
        s.execute(heads.update().values(lastRecord="ff" * 32))
    with pytest.raises(ValueError, match="head differs"):
        reconcile(store, peer, anchor)


class Server:
    def __init__(self, source, *, response_override=None):
        self.source = source
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                owner.requests.append(self.path)
                if response_override is not None:
                    response_override(self)
                    return
                url = urlsplit(self.path)
                if url.path.endswith('/checkpoint'):
                    data = source.checkpoint()
                else:
                    data = source.consistency(int(url.path.rsplit('/', 1)[1]),
                        int(parse_qs(url.query)['new_size'][0]))
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.http.server_port)
    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)


def test_real_http_exchange_with_independent_operator_and_fork_detection(setup):
    store, source, peer, _, _ = setup
    source.grow(3)
    server = Server(source)
    try:
        peer = Peer(source.pin, 'syn', server.url, allowHttp=True)
        anchor, client = Anchor(peer), PeerClient(peer, 2)
        assert poll_peer(store, peer, anchor, client)['status'] == 'accepted'
        source.grow(7)
        assert poll_peer(store, peer, anchor, client)['status'] == 'accepted'
        assert server.requests[-1].endswith('/consistency/3?new_size=7')
        fork = source.fork(7)
        source.checkpoints[7] = fork.checkpoints[7]
        assert poll_peer(store, peer, anchor, client)['status'] == 'equivocation'
    finally:
        server.close()


def test_redirects_are_not_followed(setup):
    _, source, _, _, _ = setup
    def redirect(h):
        h.send_response(302)
        h.send_header('Location', 'http://127.0.0.1:9/private')
        h.end_headers()
    server = Server(source, response_override=redirect)
    try:
        client = PeerClient(Peer(source.pin, 'syn', server.url, allowHttp=True), 1)
        with pytest.raises(PeerUnavailable):
            client.checkpoint()
        assert len(server.requests) == 1
    finally:
        server.close()


def test_public_read_only_witness_api_redacts_endpoint_and_exports_conflict(setup):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from syn.witnessing.http import build_router
    store, source, peer, anchor, sessions = setup
    source.grow(3)
    poll_peer(store, peer, anchor, source)
    fork = source.fork(3)
    poll_peer(store, peer, anchor, fork)
    app = FastAPI()
    app.include_router(build_router(sessions, store.observer, store.node_ref, WitnessConfig(peers=(peer,))))
    client = TestClient(app)
    summary = client.get('/v1/operator/dag/witnesses')
    assert summary.status_code == 200 and peer.url not in summary.text
    packet = client.get('/v1/operator/dag/witnesses/' + peer.operator + '/conflict').json()
    assert verify_equivocation(packet['first'], packet['second'], peer)
    assert client.post('/v1/operator/dag/witnesses').status_code == 405


def test_missing_ledger_receipt_is_not_accepted_as_intact_archive(setup):
    store, source, peer, anchor, sessions = setup
    source.grow(2)
    poll_peer(store, peer, anchor, source)
    with sessions.begin() as session:
        session.execute(messages.delete())
    with pytest.raises(ValueError, match='ledger receipt'):
        store.snapshot(peer)


def test_read_only_api_can_pin_consistency_target_while_latest_advances(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from syn.operator.dag_http import build_router
    engine = create_engine('sqlite:///' + str(tmp_path / 'remote.db'))
    try:
        dag.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        source = Source()
        for count in (2, 4, 6):
            source.grow(count)
        with sessions.begin() as s:
            for i, leaf in enumerate(source.leaves, 1):
                s.execute(dag.nodes.insert().values(digest=leaf, operatorHash=source.pin,
                    sequence=i, source='authority', sourceIndex=i-1, record='{}'))
            for cp in source.checkpoints.values():
                b = cp['body']
                s.execute(dag.checkpoints.insert().values(operatorHash=source.pin,
                    sequence=b['sequence'], root=b['root'], signature=cp['signature'],
                    publicKey=cp['public_key'], networkId='syn', body=dag.canonical(b).decode()))
        app = FastAPI()
        app.include_router(build_router(sessions, source.pin))
        client = TestClient(app)
        value = client.get('/v1/operator/dag/consistency/2?new_size=4')
        assert value.status_code == 200
        assert value.json()['newer']['body']['sequence'] == 4
        assert dag_evidence.verify_consistency_bundle(value.json(), expected_operator=source.pin)
    finally:
        engine.dispose()
