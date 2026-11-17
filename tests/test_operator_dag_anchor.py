"""Independent Windows-host DAG checkpoint anchoring and PostgreSQL rollback fences.

These are synthetic cross-component tests: the real Windows DPAPI transport
needs a native Windows CI acceptance run.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "host-custody"
sys.path.insert(0, str(HOST))
from syn_host_custody.core import Authority, DAG_ANCHOR_SUFFIX, root_slot


def load_modules():
    pkg = types.ModuleType("operator_anchor_test")
    pkg.__path__ = [str(ROOT / "src/syn/operator")]
    sys.modules[pkg.__name__] = pkg
    modules = {}
    for name in ("dag_proofs", "dag", "dag_evidence"):
        spec = importlib.util.spec_from_file_location(
            pkg.__name__ + "." + name, ROOT / "src/syn/operator" / (name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        modules[name] = mod
    return modules["dag"], modules["dag_evidence"]


dag, evidence = load_modules()


class Vault:
    def __init__(self, public):
        self.slots = {}
        self.public = public
    def identity(self, name):
        return b"s" * 32, {"base_public_key_hash": hashlib.sha256(self.public).hexdigest(),
                            "base_public_key": self.public.hex()}
    def get_slot(self, service, slot):
        return self.slots.get((service, slot))
    def set_slot(self, service, slot, value):
        self.slots[(service, slot)] = value


class Signer:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.public_key = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    def sign(self, payload):
        return self.key.sign(payload)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "dag.db"))
    dag.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    signer = Signer()
    pin = hashlib.sha256(signer.public_key).hexdigest()
    vault = Vault(signer.public_key)
    approvals = []
    broker = Authority(vault, approve=lambda message: approvals.append(message) or True)
    server = types.SimpleNamespace(expected_hash=pin, signer=signer,
                                   config={"network_id": "syn", "keyring_service": "syn.operator",
                                           "keyring_account": "primary"})
    src = {"authority": [], "adoption": []}
    monkeypatch.setattr(dag, "_source_records", lambda _: src)
    class Adapter:
        def read(self, _server):
            return broker.request({"op": "dag_anchor", "service": "syn.operator",
                                   "account": "primary", "pin": pin, "action": "read"})
        def advance(self, _server, packet):
            return broker.request({"op": "dag_anchor", "service": "syn.operator",
                                   "account": "primary", "pin": pin, "action": "advance",
                                   "checkpoint": packet})
    monkeypatch.setattr(dag, "_anchor_adapter", lambda: Adapter())
    yield sessions, server, src, broker, vault, approvals
    engine.dispose()


def test_host_rejects_generic_anchor_slot_access(scenario):
    _, _, _, broker, vault, _ = scenario
    slot = "primary" + DAG_ANCHOR_SUFFIX
    assert root_slot(slot)
    for op in ("get", "set"):
        req = {"op": op, "service": "syn.operator", "account": "slot", "slot": slot}
        if op == "set":
            req["value"] = "evil"
        with pytest.raises(ValueError, match="Root HD material|protected"):
            broker.request(req)
    assert not vault.slots


def test_first_anchor_requires_approval_and_advances_without_reapproval(scenario):
    sessions, server, source, broker, vault, approvals = scenario
    source["authority"].append({"pledge_offer_sha256": "11" * 32})
    assert dag.sync(sessions, server)["nodes"] == 1
    assert len(approvals) == 1
    previous = json.loads(vault.slots["syn.operator", "primary" + DAG_ANCHOR_SUFFIX])
    source["adoption"].append({"offer": "11" * 32, "kind": "selected", "body": {"choice": 1}})
    assert dag.sync(sessions, server)["nodes"] == 2
    assert len(approvals) == 1
    current = json.loads(vault.slots["syn.operator", "primary" + DAG_ANCHOR_SUFFIX])
    assert current["body"]["sequence"] == 2
    assert current["body"]["previous_checkpoint"] == evidence.checkpoint_digest(previous)
    assert dag.sync(sessions, server)["added"] == 0
    assert len(approvals) == 1


def test_deleted_database_history_is_detected_against_windows_host(scenario):
    sessions, server, source, _, _, _ = scenario
    source["authority"].append({"pledge_offer_sha256": "11" * 32})
    dag.sync(sessions, server)
    with sessions.begin() as session:
        session.execute(dag.checkpoints.delete().where(dag.checkpoints.c.sequence == 1))
        session.execute(dag.nodes.delete())
    with pytest.raises(ValueError, match="Windows anchor"):
        dag.sync(sessions, server)


def test_forged_host_anchor_and_rewound_checkpoint_rejected(scenario):
    sessions, server, source, broker, vault, _ = scenario
    source["authority"].append({"pledge_offer_sha256": "11" * 32})
    dag.sync(sessions, server)
    saved = broker.request({"op": "dag_anchor", "service": "syn.operator",
                            "account": "primary", "pin": server.expected_hash, "action": "read"})
    fake = json.loads(json.dumps(saved))
    fake["body"]["root"] = "ff" * 32
    with pytest.raises(ValueError, match="signature"):
        broker.request({"op": "dag_anchor", "service": "syn.operator", "account": "primary",
                        "pin": server.expected_hash, "action": "advance", "checkpoint": fake})
    # Even a valid old checkpoint cannot rewind the host's retained anchor.
    source["authority"].append({"pledge_offer_sha256": "22" * 32})
    dag.sync(sessions, server)
    with pytest.raises(ValueError, match="rewind|fork|skip"):
        broker.request({"op": "dag_anchor", "service": "syn.operator", "account": "primary",
                        "pin": server.expected_hash, "action": "advance", "checkpoint": saved})
    assert json.loads(vault.slots["syn.operator", "primary" + DAG_ANCHOR_SUFFIX])["body"]["sequence"] == 2


def test_host_failure_after_sql_commit_can_resume_without_resigning(scenario, monkeypatch):
    sessions, server, source, broker, vault, _ = scenario
    source["authority"].append({"pledge_offer_sha256": "11" * 32})
    adapter = dag._anchor_adapter()
    original = adapter.advance
    def fail_once(*args):
        raise RuntimeError("host unreachable")
    adapter.advance = fail_once
    monkeypatch.setattr(dag, "_anchor_adapter", lambda: adapter)
    with pytest.raises(RuntimeError, match="host unreachable"):
        dag.sync(sessions, server)
    with sessions() as session:
        assert session.execute(select(dag.nodes)).all()
    assert not vault.slots
    adapter.advance = original
    result = dag.sync(sessions, server)
    assert result["added"] == 0
    assert broker.request({"op": "dag_anchor", "service": "syn.operator",
                           "account": "primary", "pin": server.expected_hash, "action": "read"})["body"]["sequence"] == 1


def test_declined_first_anchor_does_not_write_host_state(scenario):
    sessions, server, source, broker, vault, _ = scenario
    broker.approve = lambda _: False
    source["authority"].append({"pledge_offer_sha256": "11" * 32})
    with pytest.raises(ValueError, match="declined"):
        dag.sync(sessions, server)
    assert not vault.slots
