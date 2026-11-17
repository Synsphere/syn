"""Synthetic DAG tests; Postgres trigger and Windows broker require native CI."""
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

SOURCE = Path(__file__).resolve().parents[1] / "src/syn/operator/dag.py"
spec = importlib.util.spec_from_file_location("operator_dag_under_test", SOURCE)
dag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dag)


class Signer:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.public_key = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.requests = 0

    def sign(self, payload):
        assert payload.startswith(dag.CHECKPOINT_DOMAIN)
        self.requests += 1
        return self.key.sign(payload)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "dag.db"))
    dag.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    signer = Signer()
    identity = hashlib.sha256(signer.public_key).hexdigest()
    server = SimpleNamespace(signer=signer, expected_hash=identity,
                             config={"network_id": "syn"})
    source = {"authority": [], "adoption": []}
    monkeypatch.setattr(dag, "_source_records", lambda _: source)
    yield sessions, server, source
    engine.dispose()


def test_empty_graph_reconciles_without_signing(scenario):
    sessions, server, _ = scenario
    status = dag.sync(sessions, server)
    assert status == {"operator": server.expected_hash, "nodes": 0, "added": 0,
                      "root": hashlib.sha256(b"").hexdigest(), "signed": False}
    assert server.signer.requests == 0


def test_signed_dag_replay_and_cross_source_offer_edge(scenario):
    sessions, server, source = scenario
    offer = "ab" * 32
    source["authority"].append({"pledge_offer_sha256": offer, "inheritance": 12})
    assert dag.sync(sessions, server)["added"] == 1
    source["adoption"].append({"kind": "selected", "offer": offer, "body": {"selection": 1}})
    source["adoption"].append({"kind": "granted", "offer": offer, "body": {"grant": "ff"}})
    assert dag.sync(sessions, server)["nodes"] == 3
    assert dag.sync(sessions, server)["added"] == 0
    assert server.signer.requests == 2
    with sessions() as session:
        rows = session.execute(select(dag.nodes).order_by(dag.nodes.c.sequence)).mappings().all()
    assert [r["sequence"] for r in rows] == [1, 2, 3]
    assert dag.merkle_root([r["digest"] for r in rows]) == dag.sync(sessions, server)["root"]


def test_rejects_rewritten_source_event(scenario):
    sessions, server, source = scenario
    source["authority"].append({"pledge_offer_sha256": "00" * 32, "inheritance": 12})
    dag.sync(sessions, server)
    source["authority"][0]["inheritance"] = 99
    with pytest.raises(ValueError, match="differs from keystore-verified journal"):
        dag.sync(sessions, server)


def test_rejects_changed_database_record(scenario):
    sessions, server, source = scenario
    source["authority"].append({"pledge_offer_sha256": "00" * 32, "inheritance": 12})
    dag.sync(sessions, server)
    with sessions.begin() as session:
        session.execute(dag.nodes.update().values(record='{}'))
    with pytest.raises(ValueError, match="digest|profile|Noncanonical"):
        dag.sync(sessions, server)


def test_rejects_checkpoint_tampering(scenario):
    sessions, server, source = scenario
    source["authority"].append({"pledge_offer_sha256": "00" * 32, "inheritance": 12})
    dag.sync(sessions, server)
    with sessions.begin() as session:
        session.execute(dag.checkpoints.update().where(
            dag.checkpoints.c.sequence == 1).values(root="f" * 64))
    with pytest.raises(ValueError, match="checkpoint differs"):
        dag.sync(sessions, server)


def test_failed_signature_rolls_back_leaves_and_checkpoint(scenario):
    sessions, server, source = scenario
    source["authority"].append({"pledge_offer_sha256": "00" * 32})
    original = server.signer.sign
    def fail(_):
        raise RuntimeError("host custody unavailable")
    server.signer.sign = fail
    with pytest.raises(RuntimeError, match="unavailable"):
        dag.sync(sessions, server)
    with sessions() as session:
        assert not session.execute(select(dag.nodes)).all()
    server.signer.sign = original
    assert dag.sync(sessions, server)["nodes"] == 1


def test_causal_ancestors_cannot_be_forged(scenario):
    sessions, server, source = scenario
    source["authority"].extend([{"pledge_offer_sha256": "11" * 32},
                                {"pledge_offer_sha256": "22" * 32}])
    dag.sync(sessions, server)
    with sessions.begin() as session:
        second = session.execute(select(dag.nodes).where(dag.nodes.c.sequence == 2)).mappings().one()
        edited = __import__("json").loads(second["record"])
        edited["parents"] = []
        session.execute(dag.nodes.update().where(dag.nodes.c.sequence == 2).values(
            record=dag.canonical(edited).decode("ascii"), digest=dag.digest(edited)))
    with pytest.raises(ValueError, match="noncausal"):
        dag.sync(sessions, server)


def test_checkpoint_history_is_append_only_and_fork_detected(scenario):
    sessions, server, source = scenario
    source["authority"].append({"pledge_offer_sha256": "aa" * 32})
    dag.sync(sessions, server)
    source["authority"].append({"pledge_offer_sha256": "bb" * 32})
    dag.sync(sessions, server)
    with sessions() as session:
        history = session.execute(select(dag.checkpoints).order_by(
            dag.checkpoints.c.sequence)).mappings().all()
        assert [row["sequence"] for row in history] == [0, 1, 2]
        assert len({row["root"] for row in history}) == 3
    with sessions.begin() as session:
        session.execute(dag.checkpoints.delete().where(dag.checkpoints.c.sequence == 1))
    with pytest.raises(ValueError, match="chain or payload"):
        dag.sync(sessions, server)
