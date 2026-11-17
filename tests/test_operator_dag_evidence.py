"""Proof bundles verify offline against a pinned operator; synthetic SQLite only."""
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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1] / "src/syn/operator"

# Load the real production modules without importing unrelated Syn entry points.
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

pkg = types.ModuleType("operator_evidence_test")
pkg.__path__ = [str(ROOT)]
sys.modules["operator_evidence_test"] = pkg
proofs = load("operator_evidence_test.dag_proofs", ROOT / "dag_proofs.py")
dag = load("operator_evidence_test.dag", ROOT / "dag.py")
evidence = load("operator_evidence_test.dag_evidence", ROOT / "dag_evidence.py")
http = load("operator_evidence_test.dag_http", ROOT / "dag_http.py")
witness = load("operator_evidence_test.dag_witness", ROOT / "dag_witness.py")


@pytest.fixture
def fixture(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "dag.db"))
    dag.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    operator = hashlib.sha256(public).hexdigest()
    prior = None
    with sessions.begin() as session:
        for i in range(1, 7):
            body = {"index": i, "parents": [prior] if prior else []}
            previous = dag.digest(body)
            session.execute(dag.nodes.insert().values(
                digest=previous, operatorHash=operator, sequence=i, source="authority", sourceIndex=i-1,
                record=dag.canonical(body).decode("ascii")))
            prior = previous
            if i in {2, 4, 6}:
                leaves = [x.digest for x in session.execute(
                    dag.nodes.select().where(dag.nodes.c.sequence <= i)
                    .order_by(dag.nodes.c.sequence)).all()]
                root = dag.merkle_root(leaves)
                previous_cp = None
                earlier = session.execute(dag.checkpoints.select().order_by(
                    dag.checkpoints.c.sequence.desc()).limit(1)).mappings().first()
                if earlier:
                    previous_cp = hashlib.sha256(dag.CHECKPOINT_DOMAIN + earlier["body"].encode('ascii')
                                                  + bytes.fromhex(earlier["signature"])).hexdigest()
                cpbody = dag._checkpoint_body(operator, "syn", i, root, previous_cp)
                signature = key.sign(dag.CHECKPOINT_DOMAIN + dag.canonical(cpbody))
                session.execute(dag.checkpoints.insert().values(
                    operatorHash=operator, sequence=i, root=root, signature=signature.hex(),
                    publicKey=public.hex(), networkId="syn", body=dag.canonical(cpbody).decode('ascii')))
    yield sessions, operator
    engine.dispose()


def test_exported_proofs_verify_with_only_pinned_identity(fixture):
    sessions, operator = fixture
    checkpoint = evidence.latest(sessions, operator)
    assert evidence.verify_checkpoint(checkpoint)["sequence"] == 6
    for i in range(6):
        exported = evidence.inclusion_bundle(sessions, operator, i)
        assert evidence.verify_inclusion_bundle(exported, expected_operator=operator)
        assert not any(k in exported for k in ("event", "record", "offer"))
    for old in (2, 4, 6):
        packet = evidence.consistency_bundle(sessions, operator, old)
        assert evidence.verify_consistency_bundle(packet, expected_operator=operator)
    assert evidence.checkpoint_digest(checkpoint)


def test_proof_rejects_tampering_and_wrong_identity(fixture):
    sessions, operator = fixture
    payload = evidence.inclusion_bundle(sessions, operator, 1)
    with pytest.raises(ValueError, match="Untrusted"):
        evidence.verify_inclusion_bundle(payload, expected_operator="aa"*32)
    payload["leaf_digest"] = "ff"*32
    assert not evidence.verify_inclusion_bundle(payload, expected_operator=operator)
    cp = evidence.latest(sessions, operator)
    cp["body"]["root"] = "ff"*32
    with pytest.raises(Exception):
        evidence.verify_checkpoint(cp)


def test_missing_or_truncated_graph_fails_export(fixture):
    sessions, operator = fixture
    with sessions.begin() as session:
        session.execute(dag.nodes.delete().where(dag.nodes.c.sequence == 3))
    with pytest.raises(ValueError, match="missing leaves"):
        evidence.inclusion_bundle(sessions, operator, 0)


def test_public_api_exports_only_public_evidence(fixture):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    sessions, operator = fixture
    app = FastAPI()
    app.include_router(http.build_router(sessions, operator))
    client = TestClient(app)
    checkpoint = client.get("/v1/operator/dag/checkpoint")
    assert checkpoint.status_code == 200
    assert evidence.verify_checkpoint(checkpoint.json())["sequence"] == 6
    proof = client.get("/v1/operator/dag/inclusion/0")
    assert proof.status_code == 200
    assert evidence.verify_inclusion_bundle(proof.json(), expected_operator=operator)
    consistency = client.get("/v1/operator/dag/consistency/2")
    assert consistency.status_code == 200
    assert evidence.verify_consistency_bundle(consistency.json(), expected_operator=operator)
    assert client.get("/v1/operator/dag/inclusion/999").status_code == 409
    assert client.get("/v1/operator/dag/consistency/3").status_code == 409
    assert 'record' not in proof.text and 'event' not in proof.text


def test_offline_witness_requires_independent_operator_pin(fixture, tmp_path, capsys):
    sessions, operator = fixture
    checkpoint = evidence.latest(sessions, operator)
    proof = evidence.consistency_bundle(sessions, operator, 2)
    file = tmp_path / "proof.json"
    file.write_text(json.dumps(proof))
    trusted = tmp_path / "earlier.json"
    trusted.write_text(json.dumps(proof["older"]))
    assert witness.main(["consistency", str(file), "--operator", operator,
                         "--trusted-checkpoint", str(trusted)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert witness.main(["consistency", str(file), "--operator", "aa"*32]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
    pinned = tmp_path / "wrong.json"
    pinned.write_text(json.dumps(checkpoint))
    assert witness.main(["consistency", str(file), "--operator", operator,
                         "--trusted-checkpoint", str(pinned)]) == 1
