"""Isolated contract tests: SQLite projections, not a PostgreSQL qualification.

The actual MessageStore source is loaded without unrelated governance imports.
The actual receipt protocol is unchanged. No replacement ledger algorithm is used.
PostgreSQL domain/trigger tests live separately in test_postgres.py.
"""
import ast
from datetime import UTC, datetime
from pathlib import Path
import sys
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"packages/syn-client/src"))
sys.path.insert(0, str(ROOT/"src"))


@pytest.fixture
def now():
    return datetime(2026, 11, 17, 8, 0, tzinfo=UTC)


@pytest.fixture
def ledger_module(monkeypatch):
    source = ROOT/"src/syn/communication.py"
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
    from syn.positions.store import PositionStore
    engine = create_engine("sqlite:///"+str(tmp_path/"positions.db"))
    schema.metadata.create_all(engine)
    ledger_module.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        schema.seed_types(session)
    yield PositionStore(sessions, ledger=ledger_module.MessageStore(sessions)), sessions, ledger_module
    engine.dispose()


def nmea(body):
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}\r\n"
