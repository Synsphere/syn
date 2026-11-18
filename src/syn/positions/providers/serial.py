"""Passive UART/USB-serial/RFCOMM detection using OS-backed pySerial enumeration."""
from __future__ import annotations
import os
import time
from pathlib import Path
from .base import Device, Probe, ProviderUnavailable
from .nmea import NmeaParser, sentence


def _inside_wsl():
    """WSL's enumerated legacy ttyS endpoints are not evidence of a GPS receiver."""
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="ascii").lower()
    except OSError:
        return False
    return "microsoft" in release or "wsl" in release


def probe(config, claimed=()):
    from serial.tools.list_ports import comports
    excluded = {os.path.realpath(x) for x in (*claimed, *config.excludedSerialPorts)}
    ports = {p.device: p for p in comports(include_links=True)}
    candidates = list(dict.fromkeys(config.serialPorts or sorted(ports)))
    found, seen = [], set()
    wsl_auto = not config.serialPorts and _inside_wsl()
    for path in candidates:
        real = os.path.realpath(path)
        if real in excluded or real in seen:
            continue
        seen.add(real)
        p = ports.get(path)
        # WSL often enumerates ghost legacy /dev/ttyS* ports. Do not open them
        # unless explicitly configured; genuine USB/ACM/RFCOMM ports are kept.
        if (wsl_auto and path.startswith("/dev/ttyS") and path[len("/dev/ttyS"):].isdigit()
                and getattr(p, "vid", None) is None and getattr(p, "pid", None) is None):
            continue
        found.append(Device("serial", path, getattr(p, "product", None) or getattr(p, "description", None),
            getattr(p, "serial_number", None), {"confirmed": False, "vid": getattr(p, "vid", None),
            "pid": getattr(p, "pid", None), "hwid": getattr(p, "hwid", None)}, (path,)))
    return Probe("serial", "candidates" if found else "no-devices", found,
                 "Ports are candidates until a checksum-valid NMEA sentence is observed")


def poll(device, config):
    import serial
    try:
        return _poll_serial(device, config, serial)
    except (serial.SerialException, OSError) as error:
        # A candidate port that has disappeared, is busy, or is inaccessible
        # is a normal unavailable receiver, not a failed node-level Job.
        raise ProviderUnavailable("Serial receiver is inaccessible") from error


def _poll_serial(device, config, serial):
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
