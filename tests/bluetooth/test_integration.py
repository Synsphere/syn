"""Runtime -> real codec -> native SQLite objects and receipt chains, without RF.

Only the hardware boundary is injected. Protocol encoding/decoding, scheduling,
aggregation, bounded queues, shutdown draining and persistence run together.
"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from syn.bluetooth import schema as db
from syn.bluetooth.backend import BackendHandle
from syn.bluetooth.config import BluetoothConfig
from syn.bluetooth.models import Adapter, ReceivedAdvertisement
from syn.bluetooth.protocol import build_frames
from syn.bluetooth.runtime import BluetoothRuntime
from syn.bluetooth.store import PROFILE, event_workspace
from syn.communication import heads, messages
from syn.positions.store import workspace_for
from syn_client.protocol import GENESIS_HASH, verify_receipt


NOW = datetime(2026, 11, 18, 12, 30, tzinfo=UTC)
KNOWN = "syn://thing/known_syn_peer"
UNKNOWN = "syn://thing/unknown-syn-peer"


class Hardware:
    """Assert controller exclusivity while delivering actual advertisement models."""
    def __init__(self, reports, *, advertise=True):
        self.adapter = Adapter("fake:hci0", "test", "hci0", "Injected controller",
            "epoch-1", advertise, True)
        self.reports = reports
        self.calls = []
        self.tx = self.scan = None
        self.delivered = asyncio.Event()
        self.closed = False

    async def start(self):
        self.closed = False
        self.calls.append(("open",))

    async def adapters(self):
        return [self.adapter]

    async def advertise(self, adapter, frame):
        assert self.tx is None and self.scan is None
        self.calls.append(("advertise", frame.frame_id, frame.format))
        self.tx = BackendHandle(adapter.key, adapter.connection_epoch, True,
            {"acknowledgment": "injected-native-start"})
        return self.tx

    async def stop_advertising(self, adapter, handle):
        assert self.tx is handle and self.scan is None
        self.calls.append(("stop-advertising",))
        handle.active = False
        self.tx = None

    async def start_scanning(self, adapter, callback):
        assert self.tx is None and self.scan is None
        self.calls.append(("scan",))
        self.scan = BackendHandle(adapter.key, adapter.connection_epoch, True)
        if not self.delivered.is_set():
            for report in self.reports:
                callback(report)
            self.delivered.set()
        return self.scan

    async def stop_scanning(self, adapter, handle):
        assert self.scan is handle and self.tx is None
        self.calls.append(("stop-scanning",))
        handle.active = False
        self.scan = None

    async def close(self):
        assert self.tx is None and self.scan is None
        self.calls.append(("close",))
        self.closed = True


def radio_reports(reference, source, *, at=NOW):
    return [ReceivedAdvertisement("fake:hci0", source, name="Syn", rssi=-64,
        service_uuids=frame.service_uuids, service_data=frame.service_data,
        manufacturer_data=frame.manufacturer_data, observed_at=at)
        for frame in build_frames(reference, ("syn",))]


def generic_report(source="generic-device", *, n=0, rssi=-70):
    return ReceivedAdvertisement("fake:hci0", source, name="Ordinary BLE sensor",
        rssi=rssi, manufacturer_data={0x1234: b"\x01\x02\x03"},
        observed_at=NOW + timedelta(milliseconds=n))


def config(**overrides):
    return BluetoothConfig(**{"enabled": True, "formats": ("syn", "ibeacon", "eddystone-uid"),
        "frameDurationSeconds": .1, "listenSeconds": 10,
        "startupJitterSeconds": 0, "cycleJitterSeconds": 0,
        "inventoryRefreshSeconds": .1, "retrySeconds": .1,
        "observationWindowSeconds": 10, **overrides})


def read_observations(sessions, node):
    with sessions() as session:
        rows = session.execute(select(messages).where(messages.c.workspace_id == workspace_for(node))
            .order_by(messages.c.sequence)).mappings().all()
        head = session.execute(select(heads).where(heads.c.workspace_id == workspace_for(node))).mappings().one()
    previous = GENESIS_HASH
    for number, row in enumerate(rows, 1):
        verify_receipt({"schema_version": 1, **dict(row)}, workspace_for(node))
        assert row["sequence"] == number and row["previous_hash"] == previous
        previous = row["hash"]
    assert head["sequence"] == len(rows) and head["hash"] == previous
    observations = [row for row in rows if row["payload"].get("profile") == PROFILE]
    with sessions() as session:
        for row in observations:
            event_ref = row["payload"]["eventRef"]
            event_receipt = session.execute(select(messages).where(
                messages.c.workspace_id == event_workspace(event_ref))).mappings().one()
            verify_receipt({"schema_version": 1, **dict(event_receipt)}, event_workspace(event_ref))
            assert event_receipt["payload"] == row["payload"]
            assert event_receipt["message_id"] == row["message_id"]
    return [row["payload"]["snapshot"]["knowledge"] for row in observations]


def test_runtime_beacons_receives_real_fragments_links_peer_and_drains_observations(bluetooth_state):
    store, node, sessions = bluetooth_state
    with sessions.begin() as session:
        session.execute(db.primitive["things"].insert().values(id=KNOWN,
            name="Known peer", typeRef="syn://type/device", knowledge={"owned": "elsewhere"}))
    reports = radio_reports(KNOWN, "known-radio") + radio_reports(UNKNOWN, "unknown-radio")
    reports += [generic_report(n=i, rssi=-70 + i) for i in range(3)]
    hardware = Hardware(reports)
    runtime = BluetoothRuntime(sessions, node, config(), store=store, backend=hardware)

    async def run():
        await runtime.start()
        try:
            await asyncio.wait_for(hardware.delivered.wait(), 5)
        finally:
            await asyncio.wait_for(runtime.stop(), 5)

    asyncio.run(run())
    assert runtime.fault is None and hardware.closed
    assert hardware.tx is None and hardware.scan is None
    kinds = [call[0] for call in hardware.calls]
    assert "advertise" in kinds and kinds.index("scan") > kinds.index("stop-advertising")
    assert kinds[-2:] == ["stop-scanning", "close"]
    observations = read_observations(sessions, node)
    tx = [value for value in observations if value["kind"] == "transmission-started"]
    expected_frames = build_frames(node, config().formats)
    assert len(tx) == len(expected_frames)
    assert {value["payload"]["format"] for value in tx} == {"syn", "ibeacon", "eddystone-uid"}
    assert all(value["payload"]["evidenceKind"] == "backend-callback" for value in tx)
    received = [value for value in observations if value["kind"] == "reception"]
    assert sum(value["payload"]["reportCount"] for value in received) == len(reports)
    generic = next(value for value in received if value["payload"]["advertisement"]["sourceId"] == "generic-device")
    assert generic["payload"]["reportCount"] == 3
    assert generic["payload"]["rssiMinimum"] == -70 and generic["payload"]["rssiMaximum"] == -68
    assert generic["payload"]["firstSeenAt"] == NOW.isoformat()
    assert generic["payload"]["lastSeenAt"] == (NOW + timedelta(milliseconds=2)).isoformat()
    known = next(value for value in received if value["identity"]["claimedNodeRef"] == KNOWN)
    unknown = next(value for value in received if value["identity"]["claimedNodeRef"] == UNKNOWN)
    assert known["identity"]["identityStatus"] == "claimed-known"
    assert unknown["identity"]["identityStatus"] == "claimed-unknown"
    assert all(value["identity"]["verified"] is False for value in received)
    with sessions() as session:
        assert session.execute(select(db.eventTargets.c.role).where(
            db.eventTargets.c.eventRef == known["eventRef"], db.eventTargets.c.targetRef == KNOWN)).scalar_one() == "advertised-peer"
        assert session.execute(select(db.primitive["things"].c.id).where(
            db.primitive["things"].c.id == UNKNOWN)).first() is None
    status = runtime.status()
    assert status["counts"]["reports_recorded"] == len(reports)
    assert status["pending_observations"] == 0 and status["state"] == "stopped"


def test_runtime_queue_overflow_is_persisted_with_retained_report_counts(bluetooth_state):
    store, node, sessions = bluetooth_state
    reports = [generic_report(source=f"source-{i}", n=i) for i in range(9)]
    hardware = Hardware(reports, advertise=False)
    runtime = BluetoothRuntime(sessions, node, config(maxPendingObservations=2),
        store=store, backend=hardware)

    async def run():
        await runtime.start()
        try:
            await asyncio.wait_for(hardware.delivered.wait(), 2)
        finally:
            await asyncio.wait_for(runtime.stop(), 5)

    asyncio.run(run())
    assert runtime.fault is None and hardware.closed
    observations = read_observations(sessions, node)
    received = [value for value in observations if value["kind"] == "reception"]
    losses = [value for value in observations if value["kind"] == "loss"]
    assert sum(value["payload"]["reportCount"] for value in received) == 2
    assert sum(value["payload"]["reportCount"] for value in losses) == 7
    assert all(value["payload"]["reasons"] == {"queue-full": value["payload"]["reportCount"]} for value in losses)
    assert runtime.status()["counts"]["losses_recorded"] == 7
    assert not any(call[0] == "advertise" for call in hardware.calls)


def test_reconnected_adapter_sessions_never_share_an_aggregate(bluetooth_state):
    store, node, sessions = bluetooth_state

    class ReconnectingHardware(Hardware):
        def __init__(self):
            super().__init__([], advertise=False)
            self.first_scan, self.second_scan = asyncio.Event(), asyncio.Event()

        async def start_scanning(self, adapter, callback):
            handle = await super().start_scanning(adapter, callback)
            callback(generic_report())
            (self.first_scan if adapter.connection_epoch == "epoch-1" else self.second_scan).set()
            return handle

    hardware = ReconnectingHardware()
    runtime = BluetoothRuntime(sessions, node, config(), store=store, backend=hardware)

    async def run():
        await runtime.start()
        try:
            await asyncio.wait_for(hardware.first_scan.wait(), 2)
            hardware.adapter = replace(hardware.adapter, connection_epoch="epoch-2")
            await asyncio.wait_for(hardware.second_scan.wait(), 2)
        finally:
            await asyncio.wait_for(runtime.stop(), 5)

    asyncio.run(run())
    assert runtime.fault is None and hardware.closed
    received = [value for value in read_observations(sessions, node) if value["kind"] == "reception"]
    assert len(received) == 2
    assert {value["payload"]["adapter"]["connectionEpoch"] for value in received} == {"epoch-1", "epoch-2"}
    assert all(value["payload"]["reportCount"] == 1 for value in received)


def test_cancelled_stop_still_closes_hardware_drains_writer_and_joins_all_commits(bluetooth_state):
    store, node, sessions = bluetooth_state

    class DelayedCloseHardware(Hardware):
        def __init__(self):
            super().__init__([generic_report(n=i) for i in range(3)], advertise=False)
            self.closing, self.release_close = asyncio.Event(), asyncio.Event()

        async def close(self):
            self.closing.set()
            await self.release_close.wait()
            await super().close()

    hardware = DelayedCloseHardware()
    runtime = BluetoothRuntime(sessions, node, config(), store=store, backend=hardware)

    async def run():
        await runtime.start()
        await asyncio.wait_for(hardware.delivered.wait(), 2)
        stop = asyncio.create_task(runtime.stop())
        try:
            await asyncio.wait_for(hardware.closing.wait(), 2)
            assert runtime.status()["pending_observations"] == 1
            stop.cancel()
            await asyncio.sleep(.01)
            assert not stop.done(), "stop must join native cleanup before propagating cancellation"
            hardware.release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stop, 5)
            complete = (runtime._writer is None and runtime._manager is None
                        and not runtime.status()["running"])
            before = store.ledger.head(workspace_for(node))
            await asyncio.sleep(.03)
            after = store.ledger.head(workspace_for(node))
            return complete, before, after
        finally:
            hardware.release_close.set()
            if not stop.done():
                await asyncio.gather(stop, return_exceptions=True)
            # Also clean up if the assertion exposes a regression, so a failing
            # test never leaves a background task using the fixture database.
            await runtime.stop()

    complete, before, after = asyncio.run(run())
    assert complete and hardware.closed
    assert before == after, "no database commit may outlive a completed stop"
    received = [value for value in read_observations(sessions, node) if value["kind"] == "reception"]
    assert sum(value["payload"]["reportCount"] for value in received) == 3
    assert runtime.status()["pending_observations"] == 0


def test_real_reception_transaction_failure_fences_radio_and_rolls_back_observation(bluetooth_state, monkeypatch):
    store, node, sessions = bluetooth_state
    append = store.ledger.appendInTransaction

    def fail_event_receipt(session, workspace, body):
        value = body["payload"].get("snapshot", {}).get("knowledge", {})
        if value.get("kind") == "reception" and workspace != workspace_for(node):
            raise RuntimeError("Injected Event ledger outage")
        return append(session, workspace, body)

    monkeypatch.setattr(store.ledger, "appendInTransaction", fail_event_receipt)
    hardware = Hardware([generic_report()], advertise=False)
    runtime = BluetoothRuntime(sessions, node, config(observationWindowSeconds=0),
        store=store, backend=hardware)

    async def wait_failure():
        while runtime.fault is None:
            await asyncio.sleep(.005)

    async def run():
        await runtime.start()
        try:
            await asyncio.wait_for(wait_failure(), 3)
        finally:
            await asyncio.wait_for(runtime.stop(), 5)

    asyncio.run(run())
    assert runtime.fault is not None and hardware.closed
    assert hardware.tx is None and hardware.scan is None
    assert runtime.status()["state"] == "failed"
    observations = read_observations(sessions, node)
    assert not any(value["kind"] == "reception" for value in observations)
    with sessions() as session:
        assert session.execute(select(db.primitive["events"].c.id).where(
            db.primitive["events"].c.typeRef == db.RECEPTION_TYPE)).first() is None
