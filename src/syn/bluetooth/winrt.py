"""Windows BLE publisher/watcher, using only public WinRT API capabilities.

The publisher is OS-managed, with no per-controller selector, and reserves
service UUID/data sections. Accordingly this backend advertises manufacturer
frames (including Syn iBeacon), reports service_data=False, and can receive all
OS-visible LE advertising types. It never substitutes a guessed USB adapter.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from types import SimpleNamespace
from uuid import UUID, uuid4

from .backend import (BackendCleanupError, BackendError, BackendHandle,
                      BackendUnavailable, finish_cleanup, validate_frame)
from .models import Adapter, ReceivedAdvertisement, canonical_uuid

MAX_REPORT_BYTES = 1650
MAX_SECTIONS = 64
MAX_PENDING_REPORTS = 256


def _native_api():
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        from winrt.windows.devices.bluetooth.advertisement import (
            BluetoothLEAdvertisement, BluetoothLEAdvertisementPublisher,
            BluetoothLEAdvertisementWatcher, BluetoothLEScanningMode,
            BluetoothLEManufacturerData,
        )
        from winrt.windows.storage.streams import DataReader, DataWriter
    except (ImportError, OSError) as exc:
        raise BackendUnavailable("Reinstall Syn's native Bluetooth dependencies with uv sync --frozen") from exc
    return SimpleNamespace(**locals())


def _status(value):
    return str(getattr(value, "name", value)).rsplit(".", 1)[-1].upper()


@dataclass(eq=False)
class _Publisher(BackendHandle):
    native: object = None
    token: object = None
    changed: object = None
    requested: bool = False
    stopping: bool = False
    cleanup_task: object = None


@dataclass(eq=False)
class _Watcher(BackendHandle):
    native: object = None
    received_token: object = None
    stopped_token: object = None
    stopped: object = None
    callback: object = None
    requested: bool = False
    cleanup_task: object = None


class WinRTBackend:
    def __init__(self, *, timeout=10.0, api=None):
        self.timeout = timeout
        self._api = api
        self._loop = None
        self._adapter = None
        self._fingerprint = None
        self._advertisement = None
        self._scan = None
        self._pending = deque()
        self._pending_lock = Lock()
        self._drain_scheduled = False
        self.dropped_reports = 0
        self.invalid_reports = 0
        self._cleanup_failure = None

    async def start(self):
        if self._cleanup_failure:
            raise BackendCleanupError(self._cleanup_failure)
        if self._loop is not None:
            return
        self._api = self._api or _native_api()
        self._loop = asyncio.get_running_loop()
        try:
            await self.adapters()
        except BaseException:
            self._loop = None
            raise

    async def adapters(self):
        if self._cleanup_failure:
            raise BackendCleanupError(self._cleanup_failure)
        if self._loop is None:
            raise BackendUnavailable("WinRT backend is not started")
        try:
            native = await asyncio.wait_for(self._api.BluetoothAdapter.get_default_async(), self.timeout)
        except Exception as exc:
            raise BackendUnavailable(f"Windows Bluetooth adapter unavailable: {exc}") from exc
        if native is None:
            if self._adapter is not None:
                await self._stop_owned()
            self._adapter = self._fingerprint = None
            return []
        device_id = str(native.device_id)
        le = bool(native.is_low_energy_supported)
        transmit = le and bool(native.is_peripheral_role_supported)
        receive = le and bool(native.is_central_role_supported)
        fingerprint = (device_id, transmit, receive)
        if fingerprint != self._fingerprint:
            await self._stop_owned()
            self._fingerprint = fingerprint
            self._adapter = Adapter(
                key=f"winrt:{device_id}", backend="winrt", locator=device_id,
                name="Windows default Bluetooth adapter", connection_epoch=uuid4().hex,
                can_advertise=transmit, can_scan=receive,
                service_data=False, manufacturer_data=True,
                max_advertising_data_bytes=31,
                details={"adapterSelection": "system-managed", "defaultAdapterId": device_id,
                         "scanScope": "OS-visible BLE advertisements",
                         "advertisingFormats": ["manufacturer-data"],
                         "advertisingLimitation": "WinRT reserves service UUID and service data sections",
                         "addressContinuity": "managed by Windows; not guaranteed between publications",
                         "capabilitySource": "BluetoothAdapter peripheral and central role properties"},
            )
        return [self._adapter]

    def _check_adapter(self, adapter):
        if self._cleanup_failure:
            raise BackendCleanupError(self._cleanup_failure)
        if self._loop is None or self._adapter is None or adapter != self._adapter:
            raise BackendUnavailable("Windows default Bluetooth adapter attachment changed")

    def _buffer(self, value):
        writer = self._api.DataWriter()
        try:
            writer.write_bytes(bytes(value))
            return writer.detach_buffer()
        finally:
            writer.close()

    def _read(self, buffer):
        length = int(buffer.length)
        if length < 0 or length > MAX_REPORT_BYTES:
            raise ValueError("Oversized Windows advertising buffer")
        reader = self._api.DataReader.from_buffer(buffer)
        try:
            value = bytearray(length)
            reader.read_bytes(value)
            return bytes(value)
        finally:
            reader.close()

    def _dispatch(self, callback, *args):
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(callback, *args)
            except RuntimeError:
                pass

    async def advertise(self, adapter, frame):
        self._check_adapter(adapter)
        sections = validate_frame(adapter, frame)
        if any(kind != 0xFF for kind, _ in sections):
            raise BackendError("WinRT publishing supports only manufacturer-data Syn frames")
        if self._scan is not None or self._advertisement is not None:
            raise BackendError("Stop this backend's current scan or advertisement first")
        advertisement = self._api.BluetoothLEAdvertisement()
        for company, data in frame.manufacturer_data.items():
            advertisement.manufacturer_data.append(
                self._api.BluetoothLEManufacturerData(company, self._buffer(data)))
        publisher = self._api.BluetoothLEAdvertisementPublisher(advertisement)
        handle = _Publisher(adapter.key, adapter.connection_epoch, native=publisher,
                            changed=asyncio.Event())
        self._advertisement = handle

        def changed(status, error):
            if handle.native is None:
                return
            handle.details["publisherStatus"] = status
            handle.details["error"] = error
            handle.active = _status(publisher.status) == "STARTED" and not handle.stopping
            handle.changed.set()

        def native_changed(sender, event):
            self._dispatch(changed, _status(event.status), str(getattr(event, "error", "")))

        try:
            handle.token = publisher.add_status_changed(native_changed)
            handle.requested = True
            publisher.start()

            async def wait_started():
                while True:
                    status = _status(publisher.status)
                    if status == "STARTED":
                        handle.active = True
                        return
                    if status in {"ABORTED", "STOPPED", "STOPPING"}:
                        raise BackendError(f"Windows publisher did not start: {status}")
                    handle.changed.clear()
                    await handle.changed.wait()

            await asyncio.wait_for(wait_started(), self.timeout)
            if _status(publisher.status) != "STARTED" or not handle.active:
                raise BackendError("Windows publisher stopped before start acknowledgment")
            handle.details.update({"acknowledgment": "publisher-status-started",
                                   "onAirReceptionVerified": False, "flagsManagedByOS": True,
                                   "adapterSelection": "system-managed"})
            return handle
        except asyncio.CancelledError:
            await self._stop_publisher(handle)
            raise
        except Exception as exc:
            await self._stop_publisher(handle)
            if isinstance(exc, BackendError):
                raise
            raise BackendError(f"Windows publisher failed to start: {exc}") from exc

    async def _stop_publisher(self, handle):
        handle.active = False
        handle.stopping = True
        if handle.cleanup_task is None or handle.cleanup_task.done():
            handle.cleanup_task = asyncio.create_task(self._stop_publisher_once(handle))
        await finish_cleanup(handle.cleanup_task)

    async def _stop_publisher_once(self, handle):
        handle.active = False
        handle.stopping = True
        publisher = handle.native
        if publisher is None:
            return
        try:
            if handle.requested and _status(publisher.status) not in {"STOPPED", "ABORTED"}:
                publisher.stop()

                async def stopped():
                    while _status(publisher.status) not in {"STOPPED", "ABORTED"}:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(stopped(), self.timeout)
            if handle.token is not None:
                publisher.remove_status_changed(handle.token)
                handle.token = None
        except BaseException as exc:
            self._cleanup_failure = "Windows publisher shutdown is unconfirmed; native handle retained"
            handle.details["cleanupFailure"] = self._cleanup_failure
            raise BackendCleanupError(self._cleanup_failure) from exc
        # Ownership and the COM reference survive every failure above. Only an
        # acknowledged terminal status allows another scanner/publisher to start.
        handle.native = None
        if self._advertisement is handle:
            self._advertisement = None

    async def stop_advertising(self, adapter, handle):
        if self._advertisement is handle:
            await self._stop_publisher(handle)
        handle.active = False

    def _report(self, adapter, event):
        advertisement = event.advertisement
        raw_sections = []
        service_data = {}
        total = 0
        for section in advertisement.data_sections:
            if len(raw_sections) >= MAX_SECTIONS:
                raise ValueError("Too many Windows advertising sections")
            kind = int(section.data_type)
            data = self._read(section.data)
            total += len(data)
            if total > MAX_REPORT_BYTES:
                raise ValueError("Oversized Windows advertising report")
            raw_sections.append({"type": kind, "data": data.hex()})
            size = {0x16: 2, 0x20: 4, 0x21: 16}.get(kind)
            if size is not None:
                if len(data) < size:
                    raise ValueError("Truncated Windows service data")
                uuid = (canonical_uuid(f"{int.from_bytes(data[:size], 'little'):0{size * 2}x}")
                        if size in (2, 4) else str(UUID(bytes=data[:16][::-1])))
                service_data[uuid] = data[size:]
        manufacturer_data = {}
        manufacturer_bytes = 0
        for manufacturer in advertisement.manufacturer_data:
            if len(manufacturer_data) >= MAX_SECTIONS:
                raise ValueError("Too many manufacturer sections")
            value = self._read(manufacturer.data)
            manufacturer_bytes += len(value)
            if manufacturer_bytes > MAX_REPORT_BYTES:
                raise ValueError("Oversized Windows manufacturer data report")
            manufacturer_data[int(manufacturer.company_id)] = value
        address_value = int(event.bluetooth_address)
        if not 0 <= address_value < 2 ** 48:
            raise ValueError("Invalid Bluetooth address")
        text = f"{address_value:012X}"
        address = ":".join(text[i:i + 2] for i in range(0, 12, 2))
        native_address_type = getattr(event, "bluetooth_address_type", None)
        address_type = _status(native_address_type).lower() if native_address_type is not None else "unknown"
        details = {"backend": "winrt", "adapterSelection": "system-managed",
                   "advertisementType": str(event.advertisement_type),
                   "rawSections": raw_sections, "backendDroppedReports": self.dropped_reports}
        for attr, key in (("bluetooth_address_type", "addressType"),
                          ("transmit_power_level_in_dbm", "txPowerDbm")):
            value = getattr(event, attr, None)
            if value is not None:
                details[key] = str(value) if key == "addressType" else int(value)
        return ReceivedAdvertisement(
            adapter_key=adapter.key, source_id=f"winrt:{address_type}:{address}", address=address,
            name=advertisement.local_name or None, rssi=int(event.raw_signal_strength_in_dbm),
            service_uuids=tuple(str(value) for value in advertisement.service_uuids),
            service_data=service_data, manufacturer_data=manufacturer_data,
            observed_at=datetime.now(UTC), details=details,
        )

    def _enqueue(self, handle, report):
        with self._pending_lock:
            if not handle.active:
                self.dropped_reports += 1
                return
            if len(self._pending) >= MAX_PENDING_REPORTS:
                self.dropped_reports += 1
                handle.details["droppedReports"] = self.dropped_reports
                return
            self._pending.append((handle, report))
            if self._drain_scheduled:
                return
            self._drain_scheduled = True
        self._dispatch(self._drain)

    def _drain(self):
        with self._pending_lock:
            batch = list(self._pending)
            self._pending.clear()
            self._drain_scheduled = False
        for handle, report in batch:
            if self._scan is handle and handle.active:
                handle.callback(report)
            else:
                self.dropped_reports += 1

    async def start_scanning(self, adapter, callback):
        self._check_adapter(adapter)
        if not adapter.can_scan:
            raise BackendError("Windows default adapter does not support LE central role")
        if self._scan is not None or self._advertisement is not None:
            raise BackendError("Stop this backend's current scan or advertisement first")
        watcher = self._api.BluetoothLEAdvertisementWatcher()
        # No UUID, RSSI or manufacturer filter. Passive avoids sending scan
        # requests during the listening window; scan responses are OS-dependent.
        watcher.scanning_mode = self._api.BluetoothLEScanningMode.PASSIVE
        handle = _Watcher(adapter.key, adapter.connection_epoch, native=watcher,
                          stopped=asyncio.Event(), callback=callback)
        self._scan = handle

        def received(sender, event):
            if not handle.active:
                return
            try:
                self._enqueue(handle, self._report(adapter, event))
            except (AttributeError, TypeError, ValueError, OverflowError):
                self.invalid_reports += 1
                return

        def stopped(error):
            handle.active = False
            handle.details["stoppedReason"] = error
            handle.stopped.set()

        def native_stopped(sender, event):
            self._dispatch(stopped, str(getattr(event, "error", "watcher-stopped")))

        try:
            handle.received_token = watcher.add_received(received)
            handle.stopped_token = watcher.add_stopped(native_stopped)
            handle.requested = True
            watcher.start()

            async def wait_started():
                # WinRT watcher has no Started event; poll status with a deadline.
                while _status(watcher.status) == "CREATED":
                    await asyncio.sleep(0.01)
                if _status(watcher.status) != "STARTED":
                    raise BackendError(f"Windows watcher did not start: {_status(watcher.status)}")

            await asyncio.wait_for(wait_started(), self.timeout)
            if _status(watcher.status) != "STARTED" or handle.stopped.is_set():
                raise BackendError("Windows watcher stopped before start acknowledgment")
            handle.active = True
            handle.details.update({"acknowledgment": "watcher-status-started", "mode": "passive"})
            return handle
        except asyncio.CancelledError:
            await self._stop_watcher(handle)
            raise
        except Exception as exc:
            await self._stop_watcher(handle)
            if isinstance(exc, BackendError):
                raise
            raise BackendError(f"Windows watcher failed to start: {exc}") from exc

    async def _stop_watcher(self, handle):
        handle.active = False
        if handle.cleanup_task is None or handle.cleanup_task.done():
            handle.cleanup_task = asyncio.create_task(self._stop_watcher_once(handle))
        await finish_cleanup(handle.cleanup_task)

    async def _stop_watcher_once(self, handle):
        handle.active = False
        watcher = handle.native
        if watcher is None:
            return
        try:
            if handle.requested and _status(watcher.status) not in {"STOPPED", "ABORTED"}:
                watcher.stop()
                async def stopped():
                    while _status(watcher.status) not in {"STOPPED", "ABORTED"}:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(stopped(), self.timeout)
            if handle.received_token is not None:
                watcher.remove_received(handle.received_token)
                handle.received_token = None
            if handle.stopped_token is not None:
                watcher.remove_stopped(handle.stopped_token)
                handle.stopped_token = None
        except BaseException as exc:
            self._cleanup_failure = "Windows watcher shutdown is unconfirmed; native handle retained"
            handle.details["cleanupFailure"] = self._cleanup_failure
            raise BackendCleanupError(self._cleanup_failure) from exc
        finally:
            with self._pending_lock:
                self.dropped_reports += len(self._pending)
                self._pending.clear()
        handle.native = None
        if self._scan is handle:
            self._scan = None

    async def stop_scanning(self, adapter, handle):
        if self._scan is handle:
            await self._stop_watcher(handle)
        handle.active = False

    async def _stop_owned(self):
        if self._advertisement is not None:
            await self._stop_publisher(self._advertisement)
        if self._scan is not None:
            await self._stop_watcher(self._scan)

    async def close(self):
        async def cleanup():
            await self._stop_owned()
            # Preserve the loop and references when shutdown is unconfirmed so
            # close() can retry and receive the eventual native acknowledgment.
            self._loop = None
            self._adapter = self._fingerprint = None
            self._cleanup_failure = None
            with self._pending_lock:
                self.dropped_reports += len(self._pending)
                self._pending.clear()
                self._drain_scheduled = False
        await finish_cleanup(cleanup())
