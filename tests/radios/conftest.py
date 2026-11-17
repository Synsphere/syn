"""Disposable SQLite projections and actual Syn communication hash chain."""
import ast
from datetime import UTC, datetime
from pathlib import Path
import sys
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages/syn-client/src"))
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture
def ledger_module(monkeypatch):
    source = ROOT / "src/syn/communication.py"
    tree = ast.parse(source.read_text())
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and n.module in {
        "syn.policy_adoption", "syn.governance", "syn_client.consensus"})]
    module = types.ModuleType("syn.communication")
    module.__file__ = str(source)
    exec(compile(tree, str(source), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, "syn.communication", module)
    return module


@pytest.fixture
def state(tmp_path, ledger_module):
    from syn.positions import schema
    from syn.positions.store import workspace_for
    from syn.radios.store import RadioStore
    engine = create_engine("sqlite:///" + str(tmp_path / "radio.db"))
    schema.metadata.create_all(engine)
    ledger_module.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    node = "syn://thing/radio-fixture"
    with sessions.begin() as session:
        schema.seed_types(session)
        session.execute(schema.primitive["things"].insert().values(
            id=node, name="Fixture node", typeRef="syn://type/device", knowledge={}))
        session.execute(schema.nodes.insert().values(nodeRef=node,
            ledgerWorkspace=workspace_for(node), createdAt=datetime.now(UTC), probeReport=[]))
    yield RadioStore(sessions, ledger=ledger_module.MessageStore(sessions)), node, sessions
    engine.dispose()
