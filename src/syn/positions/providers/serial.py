"""Passive UART/USB-serial/RFCOMM detection using OS-backed pySerial enumeration."""
from __future__ import annotations
import os
import time
from .base import Device, Probe
from .nmea import NmeaParser, sentence


def probe(config, claimed=()):
    from serial.tools.list_ports import comports
    excluded = {os.path.realpath(x) for x in (*claimed, *config.excludedSerialPorts)}
    ports = {p.device: p for p in comports(include_links=True)}
    candidates = list(dict.fromkeys(config.serialPorts or sorted(ports)))
    found, seen = [], set()
    for path in candidates:
        real = os.path.realpath(path)
        if real in excluded or real in seen:
            continue
        seen.add(real)
        p = ports.get(path)
        found.append(Device("serial", path, getattr(p, "product", None) or getattr(p, "description", None),
            getattr(p, "serial_number", None), {"confirmed": False, "vid": getattr(p, "vid", None),
            "pid": getattr(p, "pid", None), "hwid": getattr(p, "hwid", None)}, (path,)))
    return Probe("serial", "candidates" if found else "no-devices", found,
                 "Ports are candidates until a checksum-valid NMEA sentence is observed")


def poll(device, config):
    import serial
    baudrates = (device.options["baudrate"],) if device.options.get("confirmed") else config.serialBaudRates
    for baud in baudrates:
        # Set line state before open. Some drivers may still pulse RTS/DTR;
        # exclude sensitive hardware rather than assuming opening is side-effect-free.
        port = serial.Serial(port=None, baudrate=baud, timeout=.1, write_timeout=.1,
                             exclusive=True if os.name == "posix" else None)
        port.dtr = False
        port.rts = False
        port.port = device.address
        parser = NmeaParser(device.id, model=device.model, serialNumber=device.serialNumber, uereMeters=config.uereMeters)
        confirmed, readings, budget = False, [], 0
        try:
            port.open()
            deadline = time.monotonic()+(config.readSeconds if device.options.get("confirmed") else config.probeSeconds)
            while time.monotonic() < deadline and len(readings) < 128 and budget < 262144:
                raw = port.read_until(b"\n", 1024)
                budget += len(raw)
                if not raw:
                    continue
                try:
                    fields = sentence(raw)
                    if fields[0][-3:] in {"GGA", "RMC", "GSA", "GST", "ZDA", "GSV"}:
                        confirmed = True
                    reading = parser.feed(raw)
                    if reading is not None:
                        readings.append(reading)
                except (ValueError, UnicodeError, IndexError, OverflowError):
                    continue
        finally:
            port.close()
        if confirmed:
            return readings, {**device.options, "confirmed": True, "baudrate": baud}
    return [], dict(device.options)
