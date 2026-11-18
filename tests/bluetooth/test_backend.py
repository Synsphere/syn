"""Exercise native backend state machines with the bus/WinRT boundary replaced.

These tests do not require a radio and never send an RF packet. BlueZ export
tests use dbus-next's real signature validation; Windows tests fake its native
objects while retaining publisher/watcher lifecycle and report decoding.
"""
import asyncio
from dataclasses import replace
from enum import Enum
from types import SimpleNamespace

import pytest

from syn.bluetooth.backend import (BackendCleanupError, BackendError, BackendUnavailable,
                                   BleakScanBackend, default_backend)
from syn.bluetooth.bluez import ADAPTER, DEVICE, MANAGER, OBJECT_MANAGER, PROPERTIES, BlueZBackend, _native_api
from syn.bluetooth.models import Adapter, BeaconFrame
from syn.bluetooth.winrt import MAX_PENDING_REPORTS, WinRTBackend

UUID = "89d731bc-73f0-5cfe-88d1-f18fd051b61a"
ADAPTER_PATH = "/org/bluez/hci0"
DEVICE_PATH = ADAPTER_PATH + "/dev_AA_BB_CC_DD_EE_FF"


def service_frame():
    from uuid import UUID as U
    payload = U(UUID).bytes[::-1] + b"syn"
    return BeaconFrame("test-service", "syn", service_data={UUID: b"syn"},
                       advertising_data=b"\x02\x01\x06" + bytes([len(payload) + 1, 0x21]) + payload)


def manufacturer_frame():
    value = b"\x02\x15" + bytes.fromhex("89d731bc73f05cfe88d1f18fd051b61a") + b"\x00\x01\x00\x02\xc5"
    payload = b"\x4c\x00" + value
    return BeaconFrame("test-manufacturer", "ibeacon", manufacturer_data={0x004C: value},
                       advertising_data=b"\x02\x01\x06" + bytes([len(payload) + 1, 0xFF]) + payload)


class Bus:
    def __init__(self, api):
        self.api = api
        v = api.Variant
        self.objects = {
            ADAPTER_PATH: {ADAPTER: {"Address": v("s", "00:11:22:33:44:55"),
                                    "Powered": v("b", True), "Alias": v("s", "Radio")},
                           MANAGER: {"SupportedInstances": v("y", 2), "ActiveInstances": v("y", 0)}},
            DEVICE_PATH: {DEVICE: {"Adapter": v("o", ADAPTER_PATH),
                                   "Address": v("s", "AA:BB:CC:DD:EE:FF"),
                                   "RSSI": v("n", -55),
                                   "ServiceData": v("a{sv}", {UUID: v("ay", b"cached")})}},
        }
        self.calls = []
        self.exports = {}
        self.handlers = []
        self.disconnected = asyncio.Event()
        self.block = None
        self.block_release = asyncio.Event()
        self.entered = asyncio.Event()
        self.fail = None

    async def connect(self):
        return self

    async def wait_for_disconnect(self):
        await self.disconnected.wait()

    def disconnect(self):
        self.disconnected.set()

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_message_handler(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)

    def export(self, path, interface):
        # dbus-next validates the actual property and method signatures.
        introspection = interface.introspect()
        assert introspection.name == "org.bluez.LEAdvertisement1"
        self.exports[path] = interface

    def unexport(self, path, interface):
        self.exports.pop(path, None)

    async def call(self, message):
        self.calls.append(message)
        if message.member == self.block:
            self.entered.set()
            await self.block_release.wait()
        if message.member == self.fail or isinstance(self.fail, set) and message.member in self.fail:
            return SimpleNamespace(message_type=self.api.MessageType.ERROR,
                                   error_name="org.bluez.Error.NotPermitted", body=[])
        body = []
        if message.member == "GetNameOwner":
            body = [":1.42"]
        if message.member == "GetManagedObjects":
            body = [self.objects]
        return SimpleNamespace(message_type=self.api.MessageType.METHOD_RETURN, body=body)

    def signal(self, interface, member, body, *, path=DEVICE_PATH, sender=":1.42"):
        message = SimpleNamespace(message_type=self.api.MessageType.SIGNAL, interface=interface,
                                  member=member, path=path, sender=sender, body=body)
        for handler in list(self.handlers):
            handler(message)


def bluez():
    pytest.importorskip("dbus_next")
    api = _native_api()
    bus = Bus(api)
    return BlueZBackend(api=api, bus_factory=lambda **kw: bus, timeout=0.05), bus, api


def test_bluez_fresh_unfiltered_reports_and_owned_scan_cleanup():
    async def run():
        backend, bus, api = bluez()
        await backend.start()
        adapter = (await backend.adapters())[0]
        received = []
        handle = await backend.start_scanning(adapter, received.append)
        assert received == []  # GetManagedObjects contains a cached advertisement.
        filters = [m for m in bus.calls if m.member == "SetDiscoveryFilter"]
        assert filters[-1].body[0]["UUIDs"].value == []
        assert filters[-1].body[0]["DuplicateData"].value is True
        assert filters[-1].body[0]["Transport"].value == "le"
        v = api.Variant
        changes = {"ServiceData": v("a{sv}", {UUID: v("ay", b"fresh")}), "RSSI": v("n", -60)}
        bus.signal(PROPERTIES, "PropertiesChanged", [DEVICE, changes, []])
        bus.signal(PROPERTIES, "PropertiesChanged", [DEVICE, changes, []])
        assert len(received) == 2  # Identical fresh reports are not deduplicated.
        assert received[0].service_data[UUID] == b"fresh"
        bus.signal(PROPERTIES, "PropertiesChanged", [DEVICE, {"RSSI": v("n", -61)}, []])
        assert received[-1].service_data == {}  # Old payload is not replayed on RSSI-only changes.
        bus.signal(PROPERTIES, "PropertiesChanged", [DEVICE, changes, []], sender=":1.999")
        assert len(received) == 3
        invalid = {"ServiceData": v("a{sv}", {UUID: v("ay", b"x" * 1651)})}
        bus.signal(PROPERTIES, "PropertiesChanged", [DEVICE, invalid, []])
        assert len(received) == 3 and backend.invalid_reports == 1
        await backend.stop_scanning(adapter, handle)
        bus.signal(PROPERTIES, "PropertiesChanged", [DEVICE, changes, []])
        assert len(received) == 3
        await backend.stop_scanning(adapter, handle)
        assert [m.member for m in bus.calls].count("StopDiscovery") == 1
        assert [m for m in bus.calls if m.member == "SetDiscoveryFilter"][-1].body == [{}]
        assert not any(m.member in {"Set", "Connect", "Pair", "RemoveDevice"} for m in bus.calls)
        await backend.close()
    asyncio.run(run())


def test_bluez_controllers_have_independent_sessions_and_tx_capabilities():
    async def run():
        backend, bus, api = bluez()
        second_path = "/org/bluez/hci1"
        v = api.Variant
        bus.objects[second_path] = {
            ADAPTER: {"Address": v("s", "00:11:22:33:44:66"), "Powered": v("b", True)},
        }
        await backend.start()
        first, second = await backend.adapters()
        assert first.can_advertise and second.can_scan and not second.can_advertise
        reports_first, reports_second = [], []
        scan_first = await backend.start_scanning(first, reports_first.append)
        scan_second = await backend.start_scanning(second, reports_second.append)
        bus.signal(PROPERTIES, "PropertiesChanged", [DEVICE, {"RSSI": v("n", -60)}, []])
        bus.signal(PROPERTIES, "PropertiesChanged", [DEVICE, {"RSSI": v("n", -80)}, []],
                   path=second_path + "/dev_CC_BB_AA_DD_EE_FF")
        assert len(reports_first) == len(reports_second) == 1
        assert reports_second[0].adapter_key == second.key
        await backend.stop_scanning(first, scan_first)
        assert scan_second.active
        bus.signal(PROPERTIES, "PropertiesChanged", [DEVICE, {"RSSI": v("n", -81)}, []],
                   path=second_path + "/dev_CC_BB_AA_DD_EE_FF")
        assert len(reports_second) == 2
        assert [m.path for m in bus.calls if m.member == "StopDiscovery"] == [ADAPTER_PATH]
        await backend.close()
        assert not scan_second.active
    asyncio.run(run())


def test_bluez_export_ack_release_and_payload_validation():
    async def run():
        backend, bus, api = bluez()
        await backend.start()
        adapter = (await backend.adapters())[0]
        frame = service_frame()
        handle = await backend.advertise(adapter, frame)
        assert handle.active and handle.details["acknowledgment"] == "RegisterAdvertisement-returned"
        exported = bus.exports[handle.path]
        assert exported.Type == "broadcast"
        assert exported.ServiceData[UUID].value == b"syn"
        assert exported.ManufacturerData == {}
        exported.Release()
        assert not handle.active
        await backend.stop_advertising(adapter, handle)
        assert not bus.exports
        assert not any(m.member == "UnregisterAdvertisement" for m in bus.calls)
        with pytest.raises(BackendError, match="does not match"):
            await backend.advertise(adapter, replace(frame, advertising_data=b"\x02\x01\x06"))
        with pytest.raises(BackendError, match="fit"):
            await backend.advertise(adapter, replace(frame, advertising_data=b"x" * 32))
        await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("method,operation", [("RegisterAdvertisement", "advertise"),
                                              ("StartDiscovery", "scan")])
def test_bluez_cancelled_start_releases_our_native_request(method, operation):
    async def run():
        backend, bus, api = bluez()
        backend.timeout = 1
        await backend.start()
        adapter = (await backend.adapters())[0]
        bus.block = method
        task = asyncio.create_task(backend.advertise(adapter, service_frame()) if operation == "advertise"
                                   else backend.start_scanning(adapter, lambda report: None))
        await bus.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cleanup = "UnregisterAdvertisement" if operation == "advertise" else "StopDiscovery"
        assert cleanup in [m.member for m in bus.calls]
        assert not backend._advertisements and not backend._scans and not bus.exports
        await backend.close()
    asyncio.run(run())


def test_bluez_stop_failure_disconnects_and_hotplug_changes_epoch():
    async def run():
        backend, bus, api = bluez()
        await backend.start()
        first = (await backend.adapters())[0]
        bus.signal(OBJECT_MANAGER, "InterfacesRemoved", [ADAPTER_PATH, [ADAPTER, MANAGER]], path="/")
        second = (await backend.adapters())[0]
        assert second.key == first.key and second.connection_epoch != first.connection_epoch
        with pytest.raises(BackendUnavailable):
            await backend.advertise(first, service_frame())
        handle = await backend.advertise(second, service_frame())
        bus.fail = "UnregisterAdvertisement"
        # Native unregister failure is recoverable only after the D-Bus
        # connection actually closes, releasing all this client's sessions.
        await backend.stop_advertising(second, handle)
        assert bus.disconnected.is_set() and not handle.active
        with pytest.raises(BackendUnavailable):
            await backend.adapters()
        await backend.close()
    asyncio.run(run())


def test_bluez_daemon_loss_invalidates_live_handles():
    async def run():
        backend, bus, api = bluez()
        await backend.start()
        adapter = (await backend.adapters())[0]
        handle = await backend.start_scanning(adapter, lambda report: None)
        bus.signal("org.freedesktop.DBus", "NameOwnerChanged", ["org.bluez", ":1.42", ""],
                   path="/org/freedesktop/DBus", sender="org.freedesktop.DBus")
        assert not handle.active and bus.disconnected.is_set()
        await backend.close()
    asyncio.run(run())


def test_bluez_cancelled_connection_owns_candidate_and_waits_for_disconnect(monkeypatch):
    async def run():
        backend, bus, api = bluez()
        backend.timeout = 1
        connecting, disconnecting = asyncio.Event(), asyncio.Event()

        async def connect():
            connecting.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(bus, "connect", connect)
        monkeypatch.setattr(bus, "disconnect", disconnecting.set)
        operation = asyncio.create_task(backend.start())
        await connecting.wait()
        operation.cancel()
        await disconnecting.wait()
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and backend._closing_bus is bus
        bus.disconnected.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert backend._closing_bus is None and backend._bus is None
        await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["advertise", "scan"])
def test_bluez_unconfirmed_disconnect_retains_ownership_and_fences_retry(monkeypatch, operation):
    async def run():
        backend, bus, api = bluez()
        backend.timeout = 0.02
        await backend.start()
        adapter = (await backend.adapters())[0]
        bus.fail = ({"RegisterAdvertisement", "UnregisterAdvertisement"} if operation == "advertise"
                    else {"StartDiscovery", "StopDiscovery"})
        disconnect_requested = asyncio.Event()
        monkeypatch.setattr(bus, "disconnect", disconnect_requested.set)

        def handler_removal_failure(handler):
            raise RuntimeError("Handler removal must not skip the disconnect request")

        monkeypatch.setattr(bus, "remove_message_handler", handler_removal_failure)
        with pytest.raises(BackendCleanupError, match="retained"):
            if operation == "advertise":
                await backend.advertise(adapter, service_frame())
            else:
                await backend.start_scanning(adapter, lambda report: None)
        assert disconnect_requested.is_set()
        assert backend._closing_bus is bus and (backend._advertisements or backend._scans)
        with pytest.raises(BackendCleanupError):
            await backend.start()
        with pytest.raises(BackendCleanupError):
            await backend.start_scanning(adapter, lambda report: None)
        with pytest.raises(BackendCleanupError):
            await backend.close()
        assert backend._closing_bus is bus and not backend._disconnect_waiter.cancelled()
        bus.disconnected.set()
        await backend.close()
        assert backend._closing_bus is None and not backend._advertisements and not backend._scans
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["advertise", "scan"])
def test_bluez_repeated_cancellation_joins_owned_native_cleanup(operation):
    async def run():
        backend, bus, api = bluez()
        backend.timeout = 1
        await backend.start()
        adapter = (await backend.adapters())[0]
        if operation == "advertise":
            handle = await backend.advertise(adapter, service_frame())
            bus.block = "UnregisterAdvertisement"
            task = asyncio.create_task(backend.stop_advertising(adapter, handle))
        else:
            handle = await backend.start_scanning(adapter, lambda report: None)
            bus.block = "StopDiscovery"
            task = asyncio.create_task(backend.stop_scanning(adapter, handle))
        await bus.entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and (backend._advertisements or backend._scans)
        bus.block_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not backend._advertisements and not backend._scans
        await backend.close()
    asyncio.run(run())


def test_bluez_unexpected_disconnect_exception_still_confirms_terminal_connection(monkeypatch):
    async def run():
        backend, bus, api = bluez()

        async def disconnected():
            await bus.disconnected.wait()
            raise ConnectionError("The socket closed unexpectedly")

        monkeypatch.setattr(bus, "wait_for_disconnect", disconnected)
        await backend.start()
        adapter = (await backend.adapters())[0]
        handle = await backend.start_scanning(adapter, lambda report: None)
        bus.disconnected.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await backend.close()
        assert not handle.active and backend._cleanup_failure is None and backend._closing_bus is None
    asyncio.run(run())


class Status(Enum):
    CREATED = 0
    STARTED = 1
    STOPPED = 2
    ABORTED = 3


class Buffer:
    def __init__(self, data):
        self.data = bytes(data)
        self.length = len(self.data)


class Reader:
    @classmethod
    def from_buffer(cls, buffer):
        reader = cls()
        reader.data = buffer.data
        return reader

    def read_bytes(self, output):
        output[:] = self.data

    def close(self):
        pass


class Writer:
    def write_bytes(self, data):
        self.data = data

    def detach_buffer(self):
        return Buffer(self.data)

    def close(self):
        pass


class Publisher:
    instances = []
    start_status = Status.STARTED

    def __init__(self, advertisement):
        self.advertisement = advertisement
        self.status = Status.CREATED
        self.callback = None
        self.stopped = False
        self.instances.append(self)

    def add_status_changed(self, callback):
        self.callback = callback
        return "publisher-token"

    def remove_status_changed(self, token):
        assert token == "publisher-token"
        self.callback = None

    def start(self):
        self.status = self.start_status
        if self.callback:
            self.callback(self, SimpleNamespace(status=self.status, error="success"))

    def stop(self):
        self.stopped = True
        self.status = Status.STOPPED
        if self.callback:
            self.callback(self, SimpleNamespace(status=self.status, error="success"))


class Watcher:
    instances = []

    def __init__(self):
        self.status = Status.CREATED
        self.received = self.stopped = None
        self.instances.append(self)

    def add_received(self, callback):
        self.received = callback
        return "received-token"

    def add_stopped(self, callback):
        self.stopped = callback
        return "stopped-token"

    def remove_received(self, token):
        assert token == "received-token"
        self.received = None

    def remove_stopped(self, token):
        assert token == "stopped-token"
        self.stopped = None

    def start(self):
        self.status = Status.STARTED

    def stop(self):
        self.status = Status.STOPPED
        if self.stopped:
            self.stopped(self, SimpleNamespace(error="success"))


def winrt():
    class NativeAdapter:
        current = SimpleNamespace(device_id="test-radio", is_low_energy_supported=True,
                                  is_peripheral_role_supported=True, is_central_role_supported=True)

        @classmethod
        async def get_default_async(cls):
            return cls.current

    Publisher.instances.clear()
    Publisher.start_status = Status.STARTED
    Watcher.instances.clear()
    api = SimpleNamespace(
        BluetoothAdapter=NativeAdapter, BluetoothLEAdvertisement=lambda: SimpleNamespace(manufacturer_data=[]),
        BluetoothLEAdvertisementPublisher=Publisher, BluetoothLEAdvertisementWatcher=Watcher,
        BluetoothLEScanningMode=SimpleNamespace(PASSIVE="passive"),
        BluetoothLEManufacturerData=lambda company, data: SimpleNamespace(company_id=company, data=data),
        DataReader=Reader, DataWriter=Writer,
    )
    return WinRTBackend(api=api, timeout=0.05), api


def report_event():
    from uuid import UUID as U
    return SimpleNamespace(
        bluetooth_address=0xAABBCCDDEEFF, advertisement_type="non-connectable",
        raw_signal_strength_in_dbm=-70, bluetooth_address_type="random",
        advertisement=SimpleNamespace(
            data_sections=[SimpleNamespace(data_type=0x21, data=Buffer(U(UUID).bytes[::-1] + b"syn")),
                           SimpleNamespace(data_type=0x09, data=Buffer(b"anything"))],
            manufacturer_data=[SimpleNamespace(company_id=0x004C, data=Buffer(b"hello"))],
            service_uuids=[UUID], local_name="anything",
        ),
    )


def test_winrt_advertising_waits_for_started_and_honors_reserved_sections():
    async def run():
        backend, api = winrt()
        await backend.start()
        adapter = (await backend.adapters())[0]
        assert adapter.can_advertise and not adapter.service_data
        assert adapter.details["adapterSelection"] == "system-managed"
        with pytest.raises(BackendError, match="Service data is unsupported"):
            await backend.advertise(adapter, service_frame())
        assert Publisher.instances == []
        handle = await backend.advertise(adapter, manufacturer_frame())
        native = Publisher.instances[-1]
        assert handle.active and native.advertisement.manufacturer_data[0].company_id == 0x004C
        await backend.stop_advertising(adapter, handle)
        await asyncio.sleep(0)
        assert native.stopped and native.callback is None and not handle.active
        Publisher.start_status = Status.ABORTED
        with pytest.raises(BackendError, match="did not start"):
            await backend.advertise(adapter, manufacturer_frame())
        assert backend._advertisement is None
        await backend.close()
    asyncio.run(run())


def test_winrt_scan_decodes_reports_without_filtering_and_bounds_thread_queue():
    async def run():
        backend, api = winrt()
        await backend.start()
        adapter = (await backend.adapters())[0]
        reports = []
        handle = await backend.start_scanning(adapter, reports.append)
        watcher = Watcher.instances[-1]
        assert watcher.scanning_mode == "passive"
        event = report_event()
        watcher.received(watcher, event)
        await asyncio.sleep(0)
        assert len(reports) == 1
        assert reports[0].service_data[UUID] == b"syn"
        assert reports[0].manufacturer_data[0x004C] == b"hello"
        assert reports[0].address == "AA:BB:CC:DD:EE:FF"
        assert reports[0].source_id == "winrt:random:AA:BB:CC:DD:EE:FF"
        assert reports[0].details["rawSections"][1] == {"type": 9, "data": b"anything".hex()}
        malformed = report_event()
        malformed.advertisement.data_sections[0].data = Buffer(b"x" * 1651)
        watcher.received(watcher, malformed)
        assert backend.invalid_reports == 1
        for _ in range(MAX_PENDING_REPORTS + 3):
            watcher.received(watcher, event)
        assert backend.dropped_reports == 3
        assert len(backend._pending) == MAX_PENDING_REPORTS
        callback = watcher.received
        await backend.stop_scanning(adapter, handle)
        callback(watcher, event)
        await asyncio.sleep(0)
        assert len(reports) == 1 and not handle.active
        assert backend.dropped_reports == MAX_PENDING_REPORTS + 3
        assert watcher.received is None and watcher.stopped is None
        await backend.close()
    asyncio.run(run())


def test_winrt_hotplug_and_cancelled_start_cleanup():
    async def run():
        backend, api = winrt()
        await backend.start()
        first = (await backend.adapters())[0]
        saved = api.BluetoothAdapter.current
        api.BluetoothAdapter.current = None
        assert await backend.adapters() == []
        api.BluetoothAdapter.current = saved
        second = (await backend.adapters())[0]
        assert second.connection_epoch != first.connection_epoch
        Publisher.start_status = Status.CREATED
        task = asyncio.create_task(backend.advertise(second, manufacturer_frame()))
        while not Publisher.instances:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert Publisher.instances[-1].stopped and Publisher.instances[-1].callback is None
        assert backend._advertisement is None
        await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["advertise", "scan"])
def test_winrt_unconfirmed_start_cleanup_retains_ownership_and_fences_retry(monkeypatch, operation):
    async def run():
        backend, api = winrt()
        backend.timeout = 0.02
        await backend.start()
        adapter = (await backend.adapters())[0]
        native_class = Publisher if operation == "advertise" else Watcher
        original_stop = native_class.stop

        def broken_start(native):
            native.status = Status.STARTED
            raise OSError("Start returned an error after native activity began")

        monkeypatch.setattr(native_class, "start", broken_start)
        monkeypatch.setattr(native_class, "stop", lambda native: None)  # No terminal acknowledgment.
        with pytest.raises(BackendCleanupError, match="retained"):
            if operation == "advertise":
                await backend.advertise(adapter, manufacturer_frame())
            else:
                await backend.start_scanning(adapter, lambda report: None)
        owner = backend._advertisement if operation == "advertise" else backend._scan
        assert owner is not None and owner.native.status == Status.STARTED
        assert not isinstance(BackendCleanupError(), BackendError)
        with pytest.raises(BackendCleanupError):
            await backend.advertise(adapter, manufacturer_frame())
        with pytest.raises(BackendCleanupError):
            await backend.start_scanning(adapter, lambda report: None)
        assert len(native_class.instances) == 1
        with pytest.raises(BackendCleanupError):
            await backend.close()
        assert owner.native is not None and backend._loop is not None
        monkeypatch.setattr(native_class, "stop", original_stop)
        await backend.close()
        assert owner.native is None
        assert backend._advertisement is None and backend._scan is None and backend._loop is None
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["advertise", "scan"])
def test_winrt_repeated_cancellation_joins_native_cleanup(monkeypatch, operation):
    async def run():
        backend, api = winrt()
        backend.timeout = 1
        await backend.start()
        adapter = (await backend.adapters())[0]
        native_class = Publisher if operation == "advertise" else Watcher
        stop_requested = asyncio.Event()
        monkeypatch.setattr(native_class, "start", lambda native: None)
        monkeypatch.setattr(native_class, "stop", lambda native: stop_requested.set())
        operation_task = asyncio.create_task(
            backend.advertise(adapter, manufacturer_frame()) if operation == "advertise"
            else backend.start_scanning(adapter, lambda report: None))
        while not native_class.instances:
            await asyncio.sleep(0)
        operation_task.cancel()
        await stop_requested.wait()
        operation_task.cancel()
        await asyncio.sleep(0)
        assert not operation_task.done()
        owner = backend._advertisement if operation == "advertise" else backend._scan
        assert owner is not None and owner.native is native_class.instances[-1]
        owner.native.status = Status.STOPPED
        with pytest.raises(asyncio.CancelledError):
            await operation_task
        assert owner.native is None and backend._advertisement is None and backend._scan is None
        await backend.close()
    asyncio.run(run())


def test_macos_scan_fallback_does_not_claim_advertising():
    async def run():
        instances = []

        class Scanner:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.stopped = False
                instances.append(self)

            async def start(self):
                pass

            async def stop(self):
                self.stopped = True

        backend = BleakScanBackend(scanner_factory=Scanner)
        await backend.start()
        adapter = (await backend.adapters())[0]
        assert not adapter.can_advertise
        with pytest.raises(BackendError, match="unsupported"):
            await backend.advertise(adapter, service_frame())
        handle = await backend.start_scanning(adapter, lambda report: None)
        assert instances[-1].kwargs["service_uuids"] is None
        await backend.close()
        assert instances[-1].stopped and not handle.active
    asyncio.run(run())


def test_macos_unconfirmed_scan_cleanup_retains_native_scanner():
    async def run():
        stopped = asyncio.Event()

        class Scanner:
            def __init__(self, **kwargs):
                pass

            async def start(self):
                pass

            async def stop(self):
                await stopped.wait()

        backend = BleakScanBackend(scanner_factory=Scanner, timeout=0.01)
        await backend.start()
        adapter = (await backend.adapters())[0]
        handle = await backend.start_scanning(adapter, lambda report: None)
        with pytest.raises(BackendCleanupError):
            await backend.stop_scanning(adapter, handle)
        assert backend._scan is not None and not handle.active
        with pytest.raises(BackendCleanupError):
            await backend.start_scanning(adapter, lambda report: None)
        stopped.set()
        await backend.close()
        assert backend._scan is None
    asyncio.run(run())


def test_factory_is_lazy_and_unsupported_platform_is_explicit():
    assert isinstance(default_backend(platform="linux"), BlueZBackend)
    assert isinstance(default_backend(platform="win32"), WinRTBackend)
    assert isinstance(default_backend(platform="darwin"), BleakScanBackend)
    with pytest.raises(BackendUnavailable, match="plan9"):
        asyncio.run(default_backend(platform="plan9").start())
