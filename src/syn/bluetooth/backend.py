"""Bluetooth advertising/reporting boundary.

Backends report OS-visible LE advertisements. They do not sniff established
connections, decrypt traffic, pair, connect, or change radio power. A successful
``advertise`` means the OS accepted/started advertising, not that another radio
received a packet.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from .models import Adapter, BeaconFrame, ReceivedAdvertisement


class BackendError(RuntimeError):
    """An adapter operation failed without evidence of a successful broadcast."""


class BackendUnavailable(BackendError):
    """An optional native dependency, service or supported radio is unavailable."""


class BackendCleanupError(RuntimeError):
    """Native ownership cannot be safely released; stop the service, do not retry."""


async def finish_cleanup(operation):
    """Join a bounded cleanup operation even if its caller is cancelled again.

    A cleanup failure takes precedence over cancellation, because forgetting a
    live native handle would make the next advertising/listening cycle unsafe.
    """
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


AdvertisementCallback = Callable[[ReceivedAdvertisement], None]


class BluetoothBackend(Protocol):
    # Monotonic counters for this backend object's lifetime (including restarts).
    dropped_reports: int
    invalid_reports: int
    async def start(self) -> None: ...
    async def adapters(self) -> list[Adapter]: ...
    async def advertise(self, adapter: Adapter, frame: BeaconFrame) -> Any: ...
    async def stop_advertising(self, adapter: Adapter, handle: Any) -> None: ...
    async def start_scanning(self, adapter: Adapter, callback: AdvertisementCallback) -> Any: ...
    async def stop_scanning(self, adapter: Adapter, handle: Any) -> None: ...
    async def close(self) -> None: ...


@dataclass(eq=False)
class BackendHandle:
    adapter_key: str
    connection_epoch: str
    active: bool = False
    details: dict[str, Any] = field(default_factory=dict)


def validate_frame(adapter: Adapter, frame: BeaconFrame) -> list[tuple[int, bytes]]:
    """Validate a structured legacy frame and return its non-Flags AD sections.

    Reject hidden raw AD types and mismatches rather than silently broadcasting
    a different payload. The OS owns Flags and on-air timing. We reserve three
    bytes for Flags even if a broadcast-mode OS chooses to omit them.
    """
    if not adapter.can_advertise:
        raise BackendError("This adapter does not support the native advertising backend")
    if frame.manufacturer_data and not adapter.manufacturer_data:
        raise BackendError("Manufacturer data is unsupported on this adapter")
    if frame.service_data and not adapter.service_data:
        raise BackendError("Service data is unsupported on this adapter")
    if adapter.backend == "winrt" and (frame.service_uuids or frame.local_name is not None):
        raise BackendError("WinRT reserves service UUID lists and local-name sections")
    raw = bytes(frame.advertising_data)
    maximum = min(31, adapter.max_advertising_data_bytes)
    if not raw or len(raw) > maximum:
        raise BackendError(f"Legacy advertising payload must fit {maximum} bytes including Flags")
    from .protocol import parse_advertising_data, serialize_advertising_data
    try:
        sections = parse_advertising_data(raw)
        expected = parse_advertising_data(serialize_advertising_data(
            service_uuids=frame.service_uuids, service_data=frame.service_data,
            manufacturer_data=frame.manufacturer_data, local_name=frame.local_name))
    except ValueError as exc:
        raise BackendError(f"Malformed advertising data: {exc}") from exc
    if sorted(sections) != sorted(expected):
        raise BackendError("Raw advertising data does not match supported structured fields")
    return [section for section in sections if section[0] != 1]


def default_backend(*, platform: str | None = None, timeout: float = 10.0) -> BluetoothBackend:
    platform = platform or sys.platform
    if platform.startswith("linux"):
        from .bluez import BlueZBackend
        return BlueZBackend(timeout=timeout)
    if platform == "win32":
        from .winrt import WinRTBackend
        return WinRTBackend(timeout=timeout)
    if platform == "darwin":
        return BleakScanBackend(timeout=timeout)
    return UnavailableBackend(f"No native Bluetooth backend for {platform}")


class UnavailableBackend:
    def __init__(self, reason: str):
        self.reason = reason
        self.dropped_reports = 0
        self.invalid_reports = 0

    async def start(self):
        raise BackendUnavailable(self.reason)

    async def adapters(self):
        return []

    async def advertise(self, adapter, frame):
        raise BackendUnavailable(self.reason)

    async def start_scanning(self, adapter, callback):
        raise BackendUnavailable(self.reason)

    async def stop_advertising(self, adapter, handle):
        return None

    async def stop_scanning(self, adapter, handle):
        return None

    async def close(self):
        return None


class BleakScanBackend:
    """macOS CoreBluetooth scan fallback; no unsupported TX claim.

    CoreBluetooth and Bleak expose the OS-managed central, not independently
    selectable USB adapters. macOS may require Bluetooth access approval.
    """
    def __init__(self, *, timeout: float = 10.0, scanner_factory=None):
        self.timeout = timeout
        self._factory = scanner_factory
        self._adapter: Adapter | None = None
        self._scan = None
        self._cleanup_task = None
        self._cleanup_failure = None
        self.dropped_reports = 0
        self.invalid_reports = 0

    async def start(self):
        if self._cleanup_failure:
            raise BackendCleanupError(self._cleanup_failure)
        if self._adapter is not None:
            return
        if self._factory is None:
            try:
                from bleak import BleakScanner
            except ImportError as exc:
                raise BackendUnavailable("Reinstall Syn's native Bluetooth dependencies with uv sync --frozen") from exc
            self._factory = BleakScanner
        self._adapter = Adapter(
            "corebluetooth:default", "corebluetooth", "default", "macOS Bluetooth",
            uuid4().hex, can_advertise=False, can_scan=True,
            service_data=False, manufacturer_data=False,
            details={"adapterSelection": "system-managed", "advertising": "unsupported",
                     "scanScope": "OS-visible BLE advertisements"},
        )

    async def adapters(self):
        if self._cleanup_failure:
            raise BackendCleanupError(self._cleanup_failure)
        if self._adapter is None:
            raise BackendUnavailable("CoreBluetooth backend is not started")
        return [self._adapter]

    async def advertise(self, adapter, frame):
        raise BackendError("Custom Syn beacon advertising is unsupported by the macOS backend")

    async def stop_advertising(self, adapter, handle):
        return None

    async def start_scanning(self, adapter, callback):
        if self._cleanup_failure:
            raise BackendCleanupError(self._cleanup_failure)
        if self._adapter != adapter:
            raise BackendUnavailable("CoreBluetooth adapter attachment changed")
        if self._scan is not None:
            raise BackendError("A scanner is already running for this backend")
        handle = BackendHandle(adapter.key, adapter.connection_epoch)

        def received(device, advertisement):
            if not handle.active:
                return
            try:
                callback(ReceivedAdvertisement(
                    adapter_key=adapter.key, source_id=str(device.address),
                    address=str(device.address), name=advertisement.local_name or device.name,
                    rssi=advertisement.rssi,
                    service_uuids=tuple(advertisement.service_uuids),
                    service_data=dict(advertisement.service_data),
                    manufacturer_data=dict(advertisement.manufacturer_data),
                    observed_at=datetime.now(UTC),
                    details={"backend": "corebluetooth", "addressKind": "OS peripheral identifier",
                             "adapterSelection": "system-managed"},
                ))
            except (TypeError, ValueError, OverflowError):
                # Invalid radio input is not allowed to kill the OS callback.
                self.invalid_reports += 1
                return

        scanner = self._factory(detection_callback=received, service_uuids=None)
        self._scan = (adapter, handle, scanner)
        try:
            await asyncio.wait_for(scanner.start(), self.timeout)
            handle.active = True
            handle.details = {"acknowledgment": "scanner-started"}
            return handle
        except asyncio.CancelledError:
            await self._stop_scan()
            raise
        except Exception as exc:
            await self._stop_scan()
            raise BackendError(f"CoreBluetooth scanner failed to start: {exc}") from exc

    async def _stop_scan(self):
        if self._scan is not None:
            self._scan[1].active = False
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._stop_scan_once())
        await finish_cleanup(self._cleanup_task)

    async def _stop_scan_once(self):
        scan = self._scan
        if scan is None:
            return
        _, handle, scanner = scan
        handle.active = False
        try:
            await asyncio.wait_for(scanner.stop(), self.timeout)
        except BaseException as exc:
            self._cleanup_failure = "CoreBluetooth scanner shutdown is unconfirmed; native handle retained"
            raise BackendCleanupError(self._cleanup_failure) from exc
        self._scan = None

    async def stop_scanning(self, adapter, handle):
        if self._scan is not None and self._scan[1] is handle:
            await self._stop_scan()

    async def close(self):
        async def cleanup():
            await self._stop_scan()
            self._adapter = None
            self._cleanup_failure = None
        await finish_cleanup(cleanup())
