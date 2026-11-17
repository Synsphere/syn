"""Bluetooth LE standard Location and Navigation service, not vendor UARTs.

Only the advertised standard service is connected. No pairing or control-point
writes are attempted. LN does not itself guarantee satellite origin: these are
OS-location-reading observations with the source explicitly identified as BLE.
"""
from __future__ import annotations
import asyncio
from datetime import UTC, datetime
from .base import Device, Probe
from ..types import OS_READING, Reading, geographic

SERVICE = "00001819-0000-1000-8000-00805f9b34fb"
LOCATION = "00002a67-0000-1000-8000-00805f9b34fb"
MODEL = "00002a24-0000-1000-8000-00805f9b34fb"
SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"


def decode(data, device, *, receivedAt=None):
    data = bytes(data)
    now = receivedAt or datetime.now(UTC)
    if len(data) < 2 or len(data) > 64:
        raise ValueError("Invalid BLE LN packet size")
    flags = int.from_bytes(data[:2], "little")
    if flags & 0xE000:
        raise ValueError("Unsupported BLE LN reserved flags")
    offset = 2
    def take(size, signed=False):
        nonlocal offset
        if offset+size > len(data):
            raise ValueError("Truncated BLE LN packet")
        result = int.from_bytes(data[offset:offset+size], "little", signed=signed)
        offset += size
        return result
    telemetry = {"provider": "ble-ln", "flags": flags, "satelliteOriginKnown": False}
    if flags & 1:
        telemetry["speedMetersPerSecond"] = take(2)/100
    if flags & 2:
        telemetry["distanceMeters"] = take(3)/10
    latitude = longitude = altitude = None
    if flags & 4:
        latitude, longitude = take(4, True)/1e7, take(4, True)/1e7
    if flags & 8:
        altitude = take(3, True)/100
    if flags & 16:
        telemetry["headingDegrees"] = take(2)/100
    if flags & 32:
        telemetry["rollingTime"] = take(1)
    epoch, timecode = now, None
    if flags & 64:
        year, month, day, hour, minute, second = take(2), take(1), take(1), take(1), take(1), take(1)
        if year and month and day:
            epoch = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
            timecode = epoch.isoformat()
    if offset != len(data):
        raise ValueError("Unexpected BLE LN trailing data")
    positionStatus = (flags >> 7) & 3
    telemetry["positionStatus"] = positionStatus
    telemetry["receiverTimestampAbsent"] = timecode is None
    # Last-known, estimated or invalid positions are retained but do not replace live fixes.
    valid = latitude is not None and positionStatus == 1
    estimate = geographic(latitude, longitude, altitude, None, epoch, device.id,
        altitudeDatum="unknown" if altitude is not None else None, provenance=telemetry) if valid else None
    return Reading(device.id, epoch, now, estimate,
        "none" if not valid else "3d" if altitude is not None else "2d",
        device.model, device.serialNumber, timecode=timecode, telemetry=telemetry, typeRef=OS_READING)


async def probe_async(config):
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=config.probeSeconds, return_adv=True, service_uuids=[SERVICE])
    devices = []
    for peripheral, advertisement in found.values():
        if SERVICE in [s.lower() for s in advertisement.service_uuids]:
            devices.append(Device("ble-ln", peripheral.address, peripheral.name))
    return Probe("ble-ln", "available" if devices else "no-devices", devices[:config.maxDevices])


async def poll_async(device, config):
    from bleak import BleakClient
    readings = []
    async with BleakClient(device.address, timeout=config.probeSeconds) as client:
        model, serial = device.model, device.serialNumber
        for identifier, label in ((MODEL, "model"), (SERIAL, "serial")):
            if client.services.get_characteristic(identifier):
                raw = await client.read_gatt_char(identifier)
                value = bytes(raw)[:256].decode("utf-8", "replace")
                if label == "model":
                    model = value
                else:
                    serial = value
        # Retain descriptor identity so discovery and polling address the same job.
        def changed(_, raw):
            if len(readings) >= 128:
                return
            try:
                reading = decode(raw, device)
                from dataclasses import replace
                readings.append(replace(reading, model=model, serialNumber=serial))
            except (ValueError, OverflowError):
                pass
        await client.start_notify(LOCATION, changed)
        try:
            await asyncio.sleep(config.readSeconds)
        finally:
            await client.stop_notify(LOCATION)
    return readings


def probe(config):
    from .native import run
    return run(probe_async(config), config.probeSeconds+2)


def poll(device, config):
    from .native import run
    return run(poll_async(device, config), config.probeSeconds+config.readSeconds+3)
