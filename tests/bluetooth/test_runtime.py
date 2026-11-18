"""Hardware-boundary scheduling, reception accounting and shutdown integrity."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import threading
from types import SimpleNamespace

import pytest

from syn.bluetooth.backend import BackendError, BackendUnavailable
from syn.bluetooth.config import BluetoothConfig
from syn.bluetooth.models import Adapter, ReceivedAdvertisement
from syn.bluetooth.runtime import BluetoothRuntime


NODE = "syn://thing/test-bluetooth-node"
ADAPTER = Adapter("receiver-1", "test", "/test/1", "Test adapter", "epoch-1", True, True)


def settings(**values):
    return BluetoothConfig(**{
        "enabled": True, "formats": ("syn-ibeacon", "ibeacon"), "frameDurationSeconds": .1,
        "listenSeconds": .1, "startupJitterSeconds": 0, "cycleJitterSeconds": 0,
        "inventoryRefreshSeconds": .1, "retrySeconds": .1,
        "observationWindowSeconds": .05, **values})


async def eventually(condition, seconds=3):
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(.002)


class Store:
    def __init__(self, order=None, fail=None):
        self.events = []
        self.prepared = []
        self.order = [] if order is None else order
        self.fail = fail

    def prepare(self, node):
        self.prepared.append(node)
        return {"beaconRef": "syn://thing/test-beacon"}

    def record(self, node, kind, payload, **kwargs):
        self.order.append("record:" + kind)
        if kind == self.fail:
            raise RuntimeError("Ledger write failed")
        self.events.append((kind, deepcopy(payload), kwargs))
        return {"eventRef": "syn://event/test-" + str(len(self.events))}


class Backend:
    def __init__(self, adapters=(ADAPTER,), *, order=None):
        self.inventory = list(adapters)
        self.active = {}
        self.scans = {}
        self.calls = []
        self.order = [] if order is None else order
        self.closed = 0
        self.starts = 0
        self.start_failures = 0
        self.fail_advertise = False
        self.fail_stop_advertising = False
        self.fail_stop_scanning = False
        self.dropped_reports = 0

    async def start(self):
        self.starts += 1
        if self.start_failures:
            self.start_failures -= 1
            raise BackendUnavailable("Bluetooth service absent")

    async def adapters(self):
        return self.inventory[:]

    async def advertise(self, adapter, frame):
        assert adapter.key not in self.active and adapter.key not in self.scans
        self.order.append("hardware:start")
        self.calls.append(("advertise", adapter.key, adapter.connection_epoch, frame.format))
        if self.fail_advertise:
            raise BackendError("Start rejected")
        handle = SimpleNamespace(active=True, details={"acknowledged": True})
        self.active[adapter.key] = handle
        return handle

    async def stop_advertising(self, adapter, handle):
        self.order.append("hardware:stop")
        if self.fail_stop_advertising:
            raise BackendError("Stop not acknowledged")
        handle.active = False
        self.active.pop(adapter.key, None)
        self.calls.append(("stop-advertising", adapter.key))

    async def start_scanning(self, adapter, callback):
        assert adapter.key not in self.active and adapter.key not in self.scans
        handle = SimpleNamespace(active=True, details={})
        self.scans[adapter.key] = (handle, callback)
        self.calls.append(("scan", adapter.key, adapter.connection_epoch))
        return handle

    async def stop_scanning(self, adapter, handle):
        if self.fail_stop_scanning:
            raise BackendError("Scan stop not acknowledged")
        handle.active = False
        self.scans.pop(adapter.key, None)
        self.calls.append(("stop-scan", adapter.key))

    def emit(self, source="other-device", *, adapter=ADAPTER, rssi=-60, **values):
        report = ReceivedAdvertisement(adapter.key, source, rssi=rssi, **values)
        self.scans[adapter.key][1](report)

    async def close(self):
        for handle in self.active.values():
            handle.active = False
        for handle, _ in self.scans.values():
            handle.active = False
        self.active.clear()
        self.scans.clear()
        self.closed += 1


def test_periodic_half_duplex_intent_ack_stop_and_clean_shutdown():
    async def run():
        order = []
        store, backend = Store(order), Backend(order=order)
        runtime = BluetoothRuntime(None, NODE, settings(), store=store, backend=backend)
        await runtime.start()
        await runtime.start()
        try:
            frame_count = len(runtime._compatible_frames(ADAPTER)[0])
            await eventually(lambda: sum(kind == "transmission-started" for kind, *_ in store.events) > frame_count)
            assert backend.starts == 1 and store.prepared == [NODE]
            assert order.index("record:transmission-requested") < order.index("hardware:start")
            assert order.index("hardware:start") < order.index("record:transmission-started")
            assert order.index("hardware:stop") < order.index("record:transmission-stopped")
            assert any(call[0] == "scan" for call in backend.calls)
        finally:
            await runtime.stop()
        count = len(store.events)
        await asyncio.sleep(.01)
        assert len(store.events) == count
        assert backend.active == backend.scans == {}
        assert runtime.status()["state"] == "stopped"
        assert all(not payload.get("airReceptionVerified", False) for _, payload, _ in store.events)
    asyncio.run(run())


def test_generic_traffic_is_counted_coalesced_and_flushed_on_stop():
    async def run():
        store, backend = Store(), Backend((replace(ADAPTER, can_advertise=False),))
        runtime = BluetoothRuntime(None, NODE, settings(observationWindowSeconds=10), store=store, backend=backend)
        await runtime.start()
        await eventually(lambda: backend.scans)
        for rssi in (-63, -58, -61):
            backend.emit(rssi=rssi, name="Unrelated sensor", manufacturer_data={1234: b"\x01\x02"})
        await runtime.stop()
        received = [payload for kind, payload, _ in store.events if kind == "reception"]
        assert len(received) == 1
        report = received[0]
        assert report["reportCount"] == 3
        assert (report["rssiMinimum"], report["rssiMaximum"], report["lastRssi"]) == (-63, -58, -61)
        assert report["advertisement"]["manufacturerData"] == {"1234": "0102"}
        assert report["identity"]["isSyn"] is False
        assert runtime.status()["counts"]["reports_recorded"] == 3
    asyncio.run(run())


def test_overflow_is_durable_and_every_delivered_report_is_accounted():
    async def run():
        store, backend = Store(), Backend((replace(ADAPTER, can_advertise=False),))
        runtime = BluetoothRuntime(None, NODE, settings(maxPendingObservations=2,
            observationWindowSeconds=10), store=store, backend=backend)
        await runtime.start()
        await eventually(lambda: backend.scans)
        for number in range(7):
            backend.emit(source="source-" + str(number), manufacturer_data={1: bytes([number])})
        await runtime.stop()
        received = sum(p["reportCount"] for k, p, _ in store.events if k == "reception")
        lost = sum(p["reportCount"] for k, p, _ in store.events if k == "loss")
        assert received == 2 and lost == 5
        assert received + lost == runtime.status()["counts"]["reports_received"]
    asyncio.run(run())


def test_radio_input_that_exceeds_storage_envelope_is_loss_not_service_failure():
    async def run():
        adapter = replace(ADAPTER, can_advertise=False, details={"note": "\U0001f600" * 2000})
        store, backend = Store(), Backend((adapter,))
        runtime = BluetoothRuntime(None, NODE, settings(), store=store, backend=backend)
        await runtime.start()
        await eventually(lambda: backend.scans)
        # Four-byte Unicode expands when canonical ASCII JSON is hashed/stored.
        # Each native field fits its model bound; their total exceeds ledger input.
        data = {number: b"a" * 1600 for number in range(5)}
        backend.emit(manufacturer_data=data, details={"note": "\U0001f600" * 2000})
        await runtime.stop()
        runtime.check()
        assert not any(k == "reception" for k, *_ in store.events)
        assert any(k == "loss" and p["reasons"] == {"invalid-report": 1} for k, p, _ in store.events)
        assert runtime.status()["counts"]["reports_received"] == 1
    asyncio.run(run())


def test_backend_unavailable_recovers_without_fabricated_success():
    async def run():
        store, backend = Store(), Backend()
        backend.start_failures = 1
        runtime = BluetoothRuntime(None, NODE, settings(), store=store, backend=backend)
        await runtime.start()
        try:
            await eventually(lambda: any(k == "transmission-started" for k, *_ in store.events))
            assert backend.starts >= 2
            runtime.check()
        finally:
            await runtime.stop()
    asyncio.run(run())


def test_adapter_hotplug_releases_old_epoch_before_replacement_and_deduplicates():
    async def run():
        store, backend = Store(), Backend((ADAPTER, ADAPTER))
        runtime = BluetoothRuntime(None, NODE, settings(listenSeconds=.5), store=store, backend=backend)
        await runtime.start()
        try:
            await eventually(lambda: backend.scans)
            backend.inventory = [replace(ADAPTER, connection_epoch="epoch-2")]
            await eventually(lambda: any(c[:3] == ("advertise", ADAPTER.key, "epoch-2") for c in backend.calls))
            assert len(runtime.status()["adapters"]) == 1
            old_scan = next(i for i, c in enumerate(backend.calls) if c[0] == "stop-scan")
            new_start = next(i for i, c in enumerate(backend.calls) if c[:3] == ("advertise", ADAPTER.key, "epoch-2"))
            assert old_scan < new_start
        finally:
            await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["transmission-requested", "transmission-started", "reception"])
def test_ledger_failure_fences_and_releases_hardware(phase):
    async def run():
        store, backend = Store(fail=phase), Backend()
        runtime = BluetoothRuntime(None, NODE, settings(), store=store, backend=backend)
        await runtime.start()
        if phase == "reception":
            await eventually(lambda: backend.scans)
            backend.emit()
        await eventually(lambda: runtime.fault is not None)
        await runtime.stop()
        assert backend.active == backend.scans == {}
        assert runtime.status()["state"] == "failed"
        with pytest.raises(RuntimeError, match="failed"):
            runtime.check()
        if phase == "transmission-requested":
            assert not any(c[0] == "advertise" for c in backend.calls)
    asyncio.run(run())


def test_failed_native_start_is_logged_and_still_listens():
    async def run():
        store, backend = Store(), Backend()
        backend.fail_advertise = True
        runtime = BluetoothRuntime(None, NODE, settings(), store=store, backend=backend)
        await runtime.start()
        try:
            await eventually(lambda: backend.scans)
            assert any(k == "transmission-failed" for k, *_ in store.events)
            assert not any(k == "transmission-started" for k, *_ in store.events)
            runtime.check()
        finally:
            await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("scanner", [False, True])
def test_failed_release_is_terminal_and_never_starts_overlapping_session(scanner):
    async def run():
        store, backend = Store(), Backend()
        backend.fail_stop_advertising = not scanner
        backend.fail_stop_scanning = scanner
        runtime = BluetoothRuntime(None, NODE, settings(), store=store, backend=backend)
        await runtime.start()
        await eventually(lambda: runtime.fault is not None)
        await runtime.stop()
        expected = len(runtime._compatible_frames(ADAPTER)[0]) if scanner else 1
        assert len([c for c in backend.calls if c[0] == "advertise"]) == expected
        assert backend.active == backend.scans == {}
        if not scanner:
            assert any(k == "transmission-failed" and p.get("phase") == "stop" for k, p, _ in store.events)
    asyncio.run(run())


@pytest.mark.parametrize("fails", [False, True])
def test_shutdown_joins_inflight_database_transaction_and_retains_failure(fails):
    async def run():
        entered, release = threading.Event(), threading.Event()

        class SlowStore(Store):
            def record(self, node, kind, payload, **kwargs):
                if kind == "transmission-started":
                    entered.set()
                    assert release.wait(3)
                    if fails:
                        raise RuntimeError("Commit failed during shutdown")
                return super().record(node, kind, payload, **kwargs)

        store, backend = SlowStore(), Backend()
        runtime = BluetoothRuntime(None, NODE, settings(), store=store, backend=backend)
        await runtime.start()
        await eventually(entered.is_set)
        stopping = asyncio.create_task(runtime.stop())
        await asyncio.sleep(.03)
        assert not stopping.done()
        release.set()
        await stopping
        assert backend.active == backend.scans == {}
        assert (runtime.fault is not None) == fails
        if not fails:
            stopped = [p for k, p, _ in store.events if k == "transmission-stopped"]
            assert stopped and stopped[-1]["started"] is True
    asyncio.run(run())


def test_supported_identity_transport_selected_for_each_backend():
    runtime = BluetoothRuntime(None, NODE, settings(formats=("syn", "syn-ibeacon", "ibeacon", "eddystone-uid")),
                               store=Store(), backend=Backend())
    linux, _ = runtime._compatible_frames(ADAPTER)
    windows, rejected = runtime._compatible_frames(replace(ADAPTER, service_data=False))
    assert {f.format for f in linux} == {"syn", "ibeacon", "eddystone-uid"}
    assert {f.format for f in windows} == {"syn-ibeacon", "ibeacon"}
    assert "syn" in rejected and "eddystone-uid" in rejected


def test_hints_are_not_sent_if_full_identity_transport_cannot_run():
    runtime = BluetoothRuntime(None, NODE, settings(formats=("syn", "ibeacon")), store=Store())
    frames, rejected = runtime._compatible_frames(replace(ADAPTER, service_data=False))
    assert not frames and "complete Syn identity" in rejected["ibeacon"]


def test_disabled_service_is_side_effect_free():
    async def run():
        store, backend = Store(), Backend()
        runtime = BluetoothRuntime(None, None, store=store, backend=backend)
        await runtime.start()
        await runtime.stop()
        assert runtime.status()["state"] == "disabled"
        assert not store.events and not store.prepared and backend.starts == 0
    asyncio.run(run())


def test_native_callback_losses_are_flushed_once_on_shutdown():
    async def run():
        store, backend = Store(), Backend((replace(ADAPTER, can_advertise=False),))
        backend.invalid_reports = 0
        runtime = BluetoothRuntime(None, NODE, settings(), store=store, backend=backend)
        await runtime.start()
        await eventually(lambda: backend.scans)
        backend.dropped_reports += 3
        backend.invalid_reports += 2
        backend.emit()
        await runtime.stop()
        losses = [payload for kind, payload, _ in store.events if kind == "loss"]
        assert sum(p["reportCount"] for p in losses) == 5
        counts = runtime.status()["counts"]
        assert counts["backend_dropped_reports"] == 3 and counts["backend_invalid_reports"] == 2
        assert counts["reports_recorded"] == 1
        assert counts["losses_recorded"] == 5
    asyncio.run(run())
