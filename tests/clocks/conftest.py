"""Isolated SQLite projection tests. They do not qualify PostgreSQL or hardware."""
import ast
from datetime import UTC, datetime
from pathlib import Path
import sys
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

NODE = "syn://thing/clock-contract-test"
BOOT = "6f00d39f-4ec6-4567-8596-c176cdaf318f"
WALL = 1_790_000_000_000_000_000
MONO = 100_000_000_000


@pytest.fixture
def clock_state(tmp_path, monkeypatch):
    from syn.positions import schema as position_schema
    from syn.clocks import schema
    from syn.clocks.store import ClockStore
    from syn.positions.store import PositionStore
    from syn.positions.types import geographic
    from syn import recurrence
    root = Path(__file__).resolve().parents[2]
    source = root/"src/syn/communication.py"
    tree = ast.parse(source.read_text())
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and n.module in {
        "syn.policy_adoption", "syn.governance", "syn_client.consensus"})]
    module = types.ModuleType("syn.communication")
    module.__file__ = str(source)
    exec(compile(tree, str(source), "exec"), module.__dict__)
    monkeypatch.setitem(sys.modules, "syn.communication", module)
    engine = create_engine("sqlite:///"+str(tmp_path/"clocks.db"))
    position_schema.metadata.create_all(engine)
    module.metadata.create_all(engine)
    recurrence.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        position_schema.seed_types(session)
        schema.seed_types(session)
    ledger = module.MessageStore(sessions)
    positions = PositionStore(sessions, ledger=ledger)
    positions.bootstrap(NODE, geographic(51,0,None,100000,datetime.now(UTC),"timezone"))
    store = ClockStore(sessions, ledger=ledger)
    store.begin(NODE, BOOT, wall=WALL, mono=MONO)
    yield store, sessions, positions, module
    engine.dispose()
