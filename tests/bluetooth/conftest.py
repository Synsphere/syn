"""Disposable SQLite native projections with the actual Syn receipt ledger."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from syn.bluetooth import schema as bluetooth_schema
from syn.bluetooth.store import BluetoothStore
from syn.communication import metadata as ledger_metadata
from syn.positions.schema import seed_types
from syn.positions.store import workspace_for


@pytest.fixture
def bluetooth_state(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "bluetooth.db"))
    bluetooth_schema.metadata.create_all(engine)
    ledger_metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    node_ref = "syn://thing/bluetooth-test-node"
    with sessions.begin() as session:
        seed_types(session)
        session.execute(bluetooth_schema.primitive["things"].insert().values(
            id=node_ref, name="Bluetooth observer", typeRef="syn://type/device",
            knowledge={"fixture": True}))
        session.execute(bluetooth_schema.nodes.insert().values(nodeRef=node_ref,
            ledgerWorkspace=workspace_for(node_ref), createdAt=datetime.now(UTC), probeReport=[]))
    store = BluetoothStore(sessions)
    store.prepare(node_ref)
    yield store, node_ref, sessions
    engine.dispose()
