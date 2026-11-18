"""BlueZ D-Bus backend with per-controller advertising and discovery sessions."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from .backend import (BackendCleanupError, BackendError, BackendHandle,
                      BackendUnavailable, finish_cleanup, validate_frame)
from .models import Adapter, ReceivedAdvertisement

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
MANAGER = "org.bluez.LEAdvertisingManager1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
DBUS = "org.freedesktop.DBus"
MAX_DEVICES = 4096
MAX_BYTES = 1650
MAX_ENTRIES = 64


def _startup_problem(error, phase):
    """Identify the missing OS prerequisite without replaying unbounded errors."""
    if phase == "system-bus":
        if isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
            return ("Linux system D-Bus is unavailable. Start the dbus service in the OS running Syn; "
                    "Windows host custody does not replace Bluetooth's system bus.")
        if isinstance(error, PermissionError):
            return "Linux system D-Bus access was denied. Check this Linux account's system-bus permissions."
        if isinstance(error, TimeoutError):
            return "Linux system D-Bus connection timed out. Check the dbus service in the OS running Syn."
    if phase == "bluez-service":
        detail = str(error)
        if any(name in detail for name in ("ServiceUnknown", "NameHasNoOwner", "Spawn.")):
            return ("BlueZ is not available on the Linux system bus. Install/start the bluetooth service; "
                    "WSL also needs a Linux-visible Bluetooth controller.")
    detail = " ".join(str(error).split())[:140]
    return f"BlueZ {phase} failed ({type(error).__name__}): {detail}"


def _native_api():
    try:
        from dbus_next import BusType, Message, MessageType, Variant
        from dbus_next.aio import MessageBus
        from dbus_next.constants import PropertyAccess
        from dbus_next.service import ServiceInterface, dbus_property, method
    except ImportError as exc:
        raise BackendUnavailable("Reinstall Syn's native Bluetooth dependencies with uv sync --frozen") from exc
    return SimpleNamespace(**locals())


def _unwrap(value):
    return getattr(value, "value", value)


def _properties(values):
    return {key: _unwrap(value) for key, value in values.items()}


def _byte_map(values, *, uuid_keys=False):
    """Keep OS input bounded before constructing a validated observation."""
    from .models import canonical_uuid
    result = {}
    total = 0
    if not isinstance(values, dict) or len(values) > MAX_ENTRIES:
        raise ValueError("Invalid advertising map")
    for key, value in values.items():
        value = _unwrap(value)
        if not isinstance(value, (bytes, bytearray, list, tuple)) or len(value) > MAX_BYTES:
            raise ValueError("Invalid advertising byte array")
        raw = bytes(value)
        total += len(raw)
        if total > MAX_BYTES:
            raise ValueError("Oversized advertising report")
        if uuid_keys:
            key = canonical_uuid(key)
        elif isinstance(key, bool) or not isinstance(key, int) or not 0 <= key <= 65535:
            raise ValueError("Invalid manufacturer identifier")
        result[key] = raw
    return result


@dataclass(eq=False)
class _Scan(BackendHandle):
    path: str = ""
    callback: object = None
    requested: bool = False
    removed: bool = False
    cleanup_task: object = None


@dataclass(eq=False)
class _Advertisement(BackendHandle):
    path: str = ""
    adapter_path: str = ""
    interface: object = None
    requested: bool = False
    released: bool = False
    cleanup_task: object = None


def _advertisement_interface(api, frame, released):
    """Export only stable LEAdvertisement1 properties, never experimental Data."""
    class Advertisement(api.ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.LEAdvertisement1")

        @api.method()
        def Release(self):
            released()

        @api.dbus_property(access=api.PropertyAccess.READ)
        def Type(self) -> "s":
            return "broadcast"

        @api.dbus_property(access=api.PropertyAccess.READ)
        def ServiceUUIDs(self) -> "as":
            return list(frame.service_uuids)

        @api.dbus_property(access=api.PropertyAccess.READ)
        def ManufacturerData(self) -> "a{qv}":
            return {key: api.Variant("ay", bytes(value))
                    for key, value in frame.manufacturer_data.items()}

        @api.dbus_property(access=api.PropertyAccess.READ)
        def ServiceData(self) -> "a{sv}":
            return {key: api.Variant("ay", bytes(value)) for key, value in frame.service_data.items()}

    if frame.local_name is not None:
        class NamedAdvertisement(Advertisement):
            @api.dbus_property(access=api.PropertyAccess.READ)
            def LocalName(self) -> "s":
                return frame.local_name
        return NamedAdvertisement()
    return Advertisement()


class BlueZBackend:
    def __init__(self, *, timeout=10.0, api=None, bus_factory=None):
        self.timeout = timeout
        self._api = api
        self._bus_factory = bus_factory
        self._bus = None
        self._closing_bus = None
        self._disconnect_waiter = None
        self._cleanup_failure = None
        self._owner = None
        self._started = False
        self._disconnect_task = None
        self._adapter_objects = {}
        self._epochs = {}
        self._fingerprints = {}
        self._devices = OrderedDict()
        self._scans = {}
        self._advertisements = {}
        self.dropped_reports = 0
        self.invalid_reports = 0
        self._matches = (
            "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager'",
            "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
            "member='PropertiesChanged',path_namespace='/org/bluez'",
            "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
            "member='NameOwnerChanged',arg0='org.bluez'",
        )

    async def start(self):
        if self._cleanup_failure:
            raise BackendCleanupError(self._cleanup_failure)
        if self._closing_bus is not None:
            await finish_cleanup(self._confirm_disconnect())
        if self._started:
            return
        self._api = self._api or _native_api()
        factory = self._bus_factory or self._api.MessageBus
        phase = "system-bus"
        try:
            # Keep ownership even while connect/authentication is pending.
            self._bus = factory(bus_type=self._api.BusType.SYSTEM)
            self._disconnect_waiter = asyncio.create_task(self._bus.wait_for_disconnect())
            await asyncio.wait_for(self._bus.connect(), self.timeout)
            self._bus.add_message_handler(self._signal)
            for rule in self._matches:
                await self._call(DBUS, "/org/freedesktop/DBus", DBUS, "AddMatch", "s", [rule])
            phase = "bluez-service"
            # An ordinary method call permits the system bus's configured
            # service activation. GetNameOwner alone cannot activate BlueZ.
            # The subsequent owner/inventory checks still handle a service
            # that vanishes during startup.
            await self._call(BLUEZ, "/", OBJECT_MANAGER, "GetManagedObjects")
            owner = await self._call(DBUS, "/org/freedesktop/DBus", DBUS,
                                     "GetNameOwner", "s", [BLUEZ])
            self._owner = owner[0]
            self._started = True
            phase = "adapter-inventory"
            await self.adapters()
            self._disconnect_task = asyncio.create_task(self._watch_disconnect(self._bus))
        except asyncio.CancelledError:
            self._disconnect()
            await finish_cleanup(self._confirm_disconnect())
            raise
        except Exception as exc:
            self._disconnect()
            await finish_cleanup(self._confirm_disconnect())
            raise BackendUnavailable(_startup_problem(exc, phase)) from exc

    async def _watch_disconnect(self, bus):
        try:
            await asyncio.shield(self._disconnect_waiter)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        if self._bus is bus:
            self._disconnect()

    async def _call(self, destination, path, interface, member, signature="", body=None):
        if self._bus is None:
            raise BackendUnavailable("BlueZ backend is disconnected")
        message = self._api.Message(destination=destination, path=path, interface=interface,
                                    member=member, signature=signature, body=body or [])
        try:
            reply = await asyncio.wait_for(self._bus.call(message), self.timeout)
        except asyncio.TimeoutError as exc:
            raise BackendError(f"BlueZ {member} timed out") from exc
        except Exception as exc:
            raise BackendError(f"BlueZ {member} failed: {exc}") from exc
        if reply is None or reply.message_type == self._api.MessageType.ERROR:
            name = getattr(reply, "error_name", "NoReply")
            raise BackendError(f"BlueZ {member}: {name}")
        return reply.body

    def _disconnect(self):
        bus, self._bus = self._bus, None
        if bus is not None:
            self._closing_bus = bus
        else:
            bus = self._closing_bus
        self._started = False
        self._owner = None
        for handle in [*self._scans.values(), *self._advertisements.values()]:
            handle.active = False
            handle.details["stoppedReason"] = "backend-disconnected"
        self._adapter_objects.clear()
        self._epochs.clear()
        self._fingerprints.clear()
        self._devices.clear()
        if bus is not None:
            try:
                bus.remove_message_handler(self._signal)
            except Exception:
                pass
            try:
                bus.disconnect()
            except Exception as exc:
                self._cleanup_failure = f"BlueZ disconnect request failed: {exc}"

    async def _confirm_disconnect(self):
        if self._closing_bus is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._disconnect_waiter), self.timeout)
        except BaseException as exc:
            # dbus-next resolves this waiter exceptionally *after* finalizing
            # an unexpected disconnect. That is terminal too; a pending/cancelled
            # waiter is the case in which ownership must remain fenced.
            if (isinstance(exc, asyncio.CancelledError) or self._disconnect_waiter is None
                    or not self._disconnect_waiter.done() or self._disconnect_waiter.cancelled()):
                self._cleanup_failure = "BlueZ disconnect is unconfirmed; native connection retained"
                raise BackendCleanupError(self._cleanup_failure) from exc
            for handle in [*self._scans.values(), *self._advertisements.values()]:
                handle.details["disconnectError"] = str(exc)[:240]
        self._closing_bus = None
        self._scans.clear()
        self._advertisements.clear()
        self._cleanup_failure = None

    def _remove_adapter(self, path, *, native_removed=False):
        self._adapter_objects.pop(path, None)
        self._epochs.pop(path, None)
        self._fingerprints.pop(path, None)
        scan = self._scans.get(path)
        if scan is not None:
            scan.active = False
            scan.details["stoppedReason"] = "adapter-removed"
            scan.removed = scan.removed or native_removed
        for handle in self._advertisements.values():
            if handle.adapter_path == path:
                handle.active = False
                handle.details["stoppedReason"] = "adapter-removed"
                if native_removed:
                    handle.released = True
        for device in list(self._devices):
            if device.startswith(path + "/"):
                self._devices.pop(device, None)

    def _remember_device(self, path, properties):
        metadata = self._devices.pop(path, {})
        for key in ("Adapter", "Address", "AddressType", "Name", "Alias"):
            value = properties.get(key)
            if isinstance(value, str) and len(value) <= 1024:
                metadata[key] = value
        self._devices[path] = metadata
        while len(self._devices) > MAX_DEVICES:
            self._devices.popitem(last=False)

    async def adapters(self):
        if self._cleanup_failure:
            raise BackendCleanupError(self._cleanup_failure)
        if not self._started:
            raise BackendUnavailable("BlueZ backend is not started")
        objects = (await self._call(BLUEZ, "/", OBJECT_MANAGER, "GetManagedObjects"))[0]
        paths = {path for path, interfaces in objects.items() if ADAPTER in interfaces}
        for path in set(self._adapter_objects) - paths:
            self._remove_adapter(path, native_removed=True)
        adapters = []
        addresses = set()
        for path in sorted(paths):
            interfaces = objects[path]
            properties = _properties(interfaces[ADAPTER])
            advertising = _properties(interfaces.get(MANAGER, {}))
            address = str(properties.get("Address", path)).lower()
            if address in addresses:
                continue
            addresses.add(address)
            powered = properties.get("Powered") is True
            supported = int(advertising.get("SupportedInstances", 0))
            fingerprint = (address, powered, MANAGER in interfaces, supported)
            if self._fingerprints.get(path) != fingerprint:
                self._remove_adapter(path)
                self._epochs[path] = uuid4().hex
                self._fingerprints[path] = fingerprint
            adapter = Adapter(
                key=f"bluez:{address}", backend="bluez", locator=path,
                name=properties.get("Alias") or properties.get("Name") or path.rsplit("/", 1)[-1],
                connection_epoch=self._epochs[path],
                can_advertise=powered and supported > 0,
                can_scan=powered,
                max_advertising_data_bytes=31,
                details={"address": address, "powered": powered,
                         "advertisingInstances": supported,
                         "activeAdvertisingInstances": advertising.get("ActiveInstances", 0),
                         "adapterSelection": "per-controller",
                         "scanScope": "OS-visible BLE advertisements",
                         "capabilitySource": "BlueZ Adapter1 and LEAdvertisingManager1"},
            )
            self._adapter_objects[path] = adapter
            adapters.append(adapter)
        # Metadata is useful for later signals; querying the cache is never an
        # observation and cached service/manufacturer payloads are not replayed.
        for path, interfaces in objects.items():
            if DEVICE in interfaces:
                self._remember_device(path, _properties(interfaces[DEVICE]))
        return adapters

    def _check_adapter(self, adapter):
        if self._cleanup_failure:
            raise BackendCleanupError(self._cleanup_failure)
        current = self._adapter_objects.get(adapter.locator)
        if not self._started or current is None or current.connection_epoch != adapter.connection_epoch:
            raise BackendUnavailable("BlueZ adapter attachment changed")

    async def advertise(self, adapter, frame):
        self._check_adapter(adapter)
        validate_frame(adapter, frame)
        if adapter.locator in self._scans:
            raise BackendError("Stop this adapter's discovery session before advertising")
        if any(h.adapter_path == adapter.locator for h in self._advertisements.values()):
            raise BackendError("This backend already owns an advertisement on this adapter")
        handle = _Advertisement(adapter.key, adapter.connection_epoch,
                                path=f"/org/syn/advertisements/a{uuid4().hex}",
                                adapter_path=adapter.locator)

        def released():
            handle.released = True
            handle.active = False
            handle.details["stoppedReason"] = "bluez-release"

        handle.interface = _advertisement_interface(self._api, frame, released)
        self._advertisements[handle.path] = handle
        try:
            self._bus.export(handle.path, handle.interface)
            handle.requested = True
            await self._call(BLUEZ, adapter.locator, MANAGER, "RegisterAdvertisement",
                             "oa{sv}", [handle.path, {}])
            if handle.released or self._bus is None:
                raise BackendError("BlueZ released the advertisement before start acknowledgment")
            handle.active = True
            handle.details = {"acknowledgment": "RegisterAdvertisement-returned",
                              "onAirReceptionVerified": False, "flagsManagedByOS": True}
            return handle
        except BaseException:
            await self._cleanup_advertisement(handle)
            raise

    async def _cleanup_advertisement(self, handle):
        handle.active = False
        if handle.cleanup_task is None or handle.cleanup_task.done():
            handle.cleanup_task = asyncio.create_task(self._cleanup_advertisement_once(handle))
        await finish_cleanup(handle.cleanup_task)

    async def _cleanup_advertisement_once(self, handle):
        try:
            if self._bus is not None and handle.requested and not handle.released:
                await self._call(BLUEZ, handle.adapter_path, MANAGER,
                                 "UnregisterAdvertisement", "o", [handle.path])
        except BaseException:
            # A disconnect request is not proof of closure. Retain ownership
            # until wait_for_disconnect confirms it, or poison the backend.
            self._disconnect()
        if self._closing_bus is not None:
            await self._confirm_disconnect()
        else:
            if self._bus is not None:
                self._bus.unexport(handle.path, handle.interface)
            self._advertisements.pop(handle.path, None)

    async def stop_advertising(self, adapter, handle):
        if self._advertisements.get(handle.path) is handle:
            await self._cleanup_advertisement(handle)
        handle.active = False

    async def start_scanning(self, adapter, callback):
        self._check_adapter(adapter)
        if not adapter.can_scan:
            raise BackendError("BlueZ adapter is not powered/available for discovery")
        if adapter.locator in self._scans:
            raise BackendError("This backend already owns a discovery session on this adapter")
        if any(h.adapter_path == adapter.locator for h in self._advertisements.values()):
            raise BackendError("Stop this adapter's advertisement before scanning")
        handle = _Scan(adapter.key, adapter.connection_epoch, path=adapter.locator,
                       callback=callback)
        self._scans[adapter.locator] = handle
        try:
            discovery_filter = {
                "Transport": self._api.Variant("s", "le"),
                "UUIDs": self._api.Variant("as", []),
                "DuplicateData": self._api.Variant("b", True),
            }
            await self._call(BLUEZ, adapter.locator, ADAPTER,
                             "SetDiscoveryFilter", "a{sv}", [discovery_filter])
            handle.requested = True
            await self._call(BLUEZ, adapter.locator, ADAPTER, "StartDiscovery")
            if self._bus is None or self._scans.get(adapter.locator) is not handle:
                raise BackendError("BlueZ discovery ended before start acknowledgment")
            handle.active = True
            handle.details = {"acknowledgment": "StartDiscovery-returned", "duplicateData": True}
            return handle
        except BaseException:
            await self._cleanup_scan(handle)
            raise

    async def _cleanup_scan(self, handle):
        handle.active = False
        if handle.cleanup_task is None or handle.cleanup_task.done():
            handle.cleanup_task = asyncio.create_task(self._cleanup_scan_once(handle))
        await finish_cleanup(handle.cleanup_task)

    async def _cleanup_scan_once(self, handle):
        try:
            if self._bus is not None and not handle.removed:
                if handle.requested:
                    await self._call(BLUEZ, handle.path, ADAPTER, "StopDiscovery")
                await self._call(BLUEZ, handle.path, ADAPTER, "SetDiscoveryFilter", "a{sv}", [{}])
        except BaseException:
            self._disconnect()
        if self._closing_bus is not None:
            await self._confirm_disconnect()
        else:
            self._scans.pop(handle.path, None)

    async def stop_scanning(self, adapter, handle):
        if self._scans.get(handle.path) is handle:
            await self._cleanup_scan(handle)
        handle.active = False

    def _signal(self, message):
        if message.message_type != self._api.MessageType.SIGNAL:
            return
        try:
            if message.interface == DBUS and message.member == "NameOwnerChanged":
                if message.sender != DBUS:
                    return
                name, old, new = message.body
                if name == BLUEZ and self._owner and old == self._owner and new != old:
                    self._disconnect()
                return
            if not self._owner or message.sender != self._owner:
                return
            if message.interface == OBJECT_MANAGER:
                if message.member == "InterfacesAdded":
                    path, interfaces = message.body
                    if DEVICE in interfaces:
                        properties = _properties(interfaces[DEVICE])
                        self._remember_device(path, properties)
                        self._emit(path, properties, "InterfacesAdded")
                elif message.member == "InterfacesRemoved":
                    path, interfaces = message.body
                    if ADAPTER in interfaces or MANAGER in interfaces:
                        self._remove_adapter(path, native_removed=ADAPTER in interfaces)
                    if DEVICE in interfaces:
                        self._devices.pop(path, None)
                return
            if message.interface == PROPERTIES and message.member == "PropertiesChanged":
                interface, changed, invalidated = message.body
                if interface == DEVICE:
                    for key in invalidated:
                        self._devices.get(message.path, {}).pop(key, None)
                    properties = _properties(changed)
                    self._remember_device(message.path, properties)
                    self._emit(message.path, properties, "PropertiesChanged")
                elif interface == ADAPTER and "Powered" in changed:
                    if _unwrap(changed["Powered"]) is False:
                        self._remove_adapter(message.path)
        except (KeyError, TypeError, ValueError, OverflowError):
            # Radio and bus inputs are untrusted; malformed reports are dropped.
            self.invalid_reports += 1
            return

    def _emit(self, path, properties, signal):
        fresh = {"ManufacturerData", "ServiceData", "RSSI", "UUIDs", "AdvertisingData",
                 "AdvertisingFlags", "TxPower"}.intersection(properties)
        if not fresh:
            return
        metadata = self._devices.get(path, {})
        adapter_path = metadata.get("Adapter") or path.rsplit("/", 1)[0]
        scan = self._scans.get(adapter_path)
        if scan is None or not scan.active:
            return
        address = metadata.get("Address")
        details = {"backend": "bluez", "signal": signal, "changedProperties": sorted(fresh),
                   "payloadScope": "only-fields-in-this-signal", "devicePath": path}
        if "AddressType" in metadata:
            details["addressType"] = metadata["AddressType"]
        if "TxPower" in properties:
            details["txPowerDbm"] = properties["TxPower"]
        if "AdvertisingFlags" in properties:
            flags = bytes(properties["AdvertisingFlags"])
            if len(flags) <= MAX_BYTES:
                details["advertisingFlags"] = flags.hex()
        if "AdvertisingData" in properties:
            details["advertisingData"] = {
                str(key): value.hex() for key, value in _byte_map(properties["AdvertisingData"]).items()}
        observation = ReceivedAdvertisement(
            adapter_key=scan.adapter_key,
            source_id=f"bluez:{metadata.get('AddressType', 'unknown')}:{address or path}", address=address,
            name=metadata.get("Name") or metadata.get("Alias"),
            rssi=properties.get("RSSI"), service_uuids=tuple(properties.get("UUIDs", ())),
            service_data=_byte_map(properties.get("ServiceData", {}), uuid_keys=True),
            manufacturer_data=_byte_map(properties.get("ManufacturerData", {})),
            observed_at=datetime.now(UTC), details=details,
        )
        scan.callback(observation)

    async def close(self):
        await finish_cleanup(self._close())

    async def _close(self):
        try:
            for handle in list(self._advertisements.values()):
                await self._cleanup_advertisement(handle)
            for handle in list(self._scans.values()):
                await self._cleanup_scan(handle)
        finally:
            task = self._disconnect_task
            self._disconnect()
            await self._confirm_disconnect()
            self._disconnect_task = None
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
