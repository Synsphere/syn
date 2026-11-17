"""Native services. Permission denial is a diagnostic, not a permission bypass.

ModemManager reads already-enabled GPS sources; it does not reconfigure modems.
Windows and macOS fused locations are OS observations unless satellite origin
is explicitly identified by the driver. Unknown hardware identifiers stay null.
"""
from __future__ import annotations
import asyncio
from datetime import UTC, datetime, timedelta
import sys
import time
from .base import Device, Probe, ProviderUnavailable
from .nmea import NmeaParser
from ..types import GPS_READING, OS_READING, Reading, geographic, timestamp

MM = "org.freedesktop.ModemManager1"
LOCATION = MM+".Modem.Location"
MODEM = MM+".Modem"


def unwrap(value):
    if hasattr(value, "value"):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [unwrap(v) for v in value]
    return value


async def modem_bus():
    from dbus_next.aio import MessageBus
    from dbus_next import BusType
    return await MessageBus(bus_type=BusType.SYSTEM).connect()


async def modem_probe(config):
    bus = await modem_bus()
    try:
        path = "/org/freedesktop/ModemManager1"
        introspection = await bus.introspect(MM, path)
        obj = bus.get_proxy_object(MM, path, introspection)
        managed = unwrap(await obj.get_interface("org.freedesktop.DBus.ObjectManager").call_get_managed_objects())
        devices, disabled, claimed = [], [], []
        for path, interfaces in managed.items():
            props = interfaces.get(LOCATION, {})
            modem = interfaces.get(MODEM, {})
            ports = tuple("/dev/"+p[0] for p in modem.get("Ports", []))
            claimed.extend(ports)
            if props.get("Capabilities", 0) & 6:
                enabled = bool(props.get("Enabled", 0) & 6)
                devices.append(Device("modemmanager", path, modem.get("Model"),
                    None, {"enabled": enabled, "equipmentIdentifier": modem.get("EquipmentIdentifier")}, ports))
                if not enabled:
                    disabled.append(path)
        return Probe("modemmanager", "available" if devices else "no-devices", devices,
                     "GPS requires administrator enablement for: "+", ".join(disabled) if disabled else None), tuple(claimed)
    finally:
        bus.disconnect()


async def modem_poll(device, config):
    bus = await modem_bus()
    try:
        introspection = await bus.introspect(MM, device.address)
        obj = bus.get_proxy_object(MM, device.address, introspection)
        values = unwrap(await obj.get_interface(LOCATION).call_get_location())
        now, output = datetime.now(UTC), []
        # Prefer full dated NMEA when available, otherwise raw UTC position.
        if 4 in values:
            parser = NmeaParser(device.id, model=device.model, serialNumber=device.serialNumber, uereMeters=config.uereMeters)
            lines = str(values[4]).splitlines()
            lines.sort(key=lambda line: 0 if line[3:6] in {"RMC", "ZDA"} else 1)
            for raw in lines[:256]:
                try:
                    value = parser.feed(raw, receivedAt=now)
                    if value:
                        output.append(value)
                except (ValueError, UnicodeError, IndexError):
                    continue
        elif 2 in values:
            raw = values[2]
            code = raw.get("utc-time", "")
            # ModemManager raw utc-time is HHMMSS.sss, not a dated timestamp.
            parser = NmeaParser(device.id)
            epoch, _ = parser.epoch(code, now)
            estimate = geographic(raw["latitude"], raw["longitude"], raw.get("altitude"), None,
                epoch, device.id, altitudeDatum="unknown" if raw.get("altitude") is not None else None,
                provenance={"provider": "modemmanager", "dateInferred": True})
            output.append(Reading(device.id, epoch, now, estimate,
                "3d" if raw.get("altitude") is not None else "2d", device.model, device.serialNumber,
                timecode=code, telemetry={"dateInferred": True}))
        return output
    finally:
        bus.disconnect()


async def windows_poll(device, config):
    from winrt.windows.devices.geolocation import Geolocator, PositionSource
    locator = Geolocator()
    position = await locator.get_geoposition_async(timedelta(seconds=0), timedelta(seconds=config.readSeconds))
    c = position.coordinate
    now = datetime.now(UTC)
    if getattr(c, "is_remote_source", False):
        raise ProviderUnavailable("Remote/redirected Windows location is not a local receiver")
    epoch = timestamp(getattr(c, "position_source_timestamp", None) or c.timestamp)
    point = c.point.position
    satellite = c.position_source == PositionSource.SATELLITE
    telemetry = {"provider": "windows", "positionSource": int(c.position_source)}
    data = getattr(c, "satellite_data", None)
    if data is not None:
        for public, attr in (("hdop", "horizontal_dilution_of_precision"),
                             ("vdop", "vertical_dilution_of_precision"),
                             ("pdop", "position_dilution_of_precision")):
            val = getattr(data, attr, None)
            if val is not None:
                telemetry[public] = val
    estimate = geographic(point.latitude, point.longitude, point.altitude, c.accuracy,
        epoch, device.id, uncertaintyMethod="windows-reported-accuracy", altitudeDatum="unknown",
        verticalError=getattr(c, "altitude_accuracy", None), provenance=telemetry)
    return [Reading(device.id, epoch, now, estimate, "3d", timecode=epoch.isoformat(), telemetry=telemetry,
                    typeRef=GPS_READING if satellite else OS_READING)]


def macos_poll(device, config):
    import CoreLocation
    from Foundation import NSDate, NSRunLoop
    manager = CoreLocation.CLLocationManager.alloc().init()
    status = manager.authorizationStatus()
    if status not in (3, 4):
        raise ProviderUnavailable("Core Location permission must be granted by the user first")
    manager.startUpdatingLocation()
    deadline = time.monotonic()+config.readSeconds
    try:
        while time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.05))
            location = manager.location()
            if location is None or location.horizontalAccuracy() < 0:
                continue
            epoch = datetime.fromtimestamp(location.timestamp().timeIntervalSince1970(), UTC)
            now = datetime.now(UTC)
            if abs((now-epoch).total_seconds()) > config.maxAgeSeconds:
                continue
            coordinate = location.coordinate()
            vertical = location.verticalAccuracy()
            estimate = geographic(coordinate.latitude, coordinate.longitude,
                location.altitude() if vertical >= 0 else None, location.horizontalAccuracy(), epoch, device.id,
                uncertaintyMethod="core-location-reported-accuracy", verticalError=vertical if vertical >= 0 else None,
                altitudeDatum="mean-sea-level" if vertical >= 0 else None,
                provenance={"provider": "core-location", "satelliteOriginKnown": False})
            return [Reading(device.id, epoch, now, estimate, "3d" if vertical >= 0 else "2d",
                            timecode=epoch.isoformat(), typeRef=OS_READING)]
        return []
    finally:
        manager.stopUpdatingLocation()


def run(coroutine, timeout):
    async def bounded():
        return await asyncio.wait_for(coroutine, timeout)
    return asyncio.run(bounded())


def probe(config):
    if sys.platform.startswith("linux"):
        return run(modem_probe(config), config.probeSeconds+1)
    if sys.platform == "win32":
        from winrt.windows.devices.geolocation import Geolocator
        return Probe("windows", "available", [Device("windows", "local-geolocator")]), ()
    if sys.platform == "darwin":
        import CoreLocation
        return Probe("core-location", "available", [Device("core-location", "local-core-location")]), ()
    return Probe("native", "unsupported-platform"), ()


def poll(device, config):
    if device.provider == "modemmanager":
        return run(modem_poll(device, config), config.readSeconds+1)
    if device.provider == "windows":
        return run(windows_poll(device, config), config.readSeconds+1)
    if device.provider == "core-location":
        return macos_poll(device, config)
    raise ValueError("Unknown native location provider")
