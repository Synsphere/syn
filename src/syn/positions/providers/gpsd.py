"""gpsd JSON driver: includes devices whose native protocol is not NMEA."""
from __future__ import annotations
from datetime import UTC, datetime
import ipaddress
import json
import math
import socket
import time
from urllib.parse import urlsplit

from .base import Device, Probe
from ..types import Reading, finite, geographic, timestamp


def endpoint(address: str):
    value = urlsplit("tcp://"+address)
    if value.username or value.password or value.path or value.query or value.fragment:
        raise ValueError("GPS endpoints are numeric-IP:port, without credentials or paths")
    host = str(ipaddress.ip_address(value.hostname or ""))
    if not value.port:
        raise ValueError("GPS endpoint needs a port")
    return host, value.port


def messages(address, command, seconds):
    deadline = time.monotonic()+seconds
    with socket.create_connection(endpoint(address), timeout=seconds) as stream:
        stream.sendall(command.encode("ascii"))
        pending, total, count = bytearray(), 0, 0
        while time.monotonic() < deadline and count < 256:
            stream.settimeout(max(.01, deadline-time.monotonic()))
            try:
                block = stream.recv(8192)
            except TimeoutError:
                break
            except ConnectionResetError:
                if count:
                    break  # Keep complete observations received before disconnect.
                raise
            if not block:
                break
            total += len(block)
            if total > 1_048_576:
                raise ValueError("gpsd response exceeds one MiB")
            pending.extend(block)
            while b"\n" in pending and count < 256:
                raw, _, pending = pending.partition(b"\n")
                if len(raw) > 65536:
                    raise ValueError("Oversized gpsd record")
                try:
                    record = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
                except (ValueError, UnicodeError):
                    continue
                if isinstance(record, dict):
                    count += 1
                    yield record
            if len(pending) > 65536:
                raise ValueError("Unterminated gpsd record")


def probe(address, config):
    found = []
    for value in messages(address, "?DEVICES;\n", config.probeSeconds):
        if value.get("class") == "DEVICES":
            for item in value.get("devices", [])[:config.maxDevices]:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    path = item["path"]
                    found.append(Device("gpsd", address+"|"+path, item.get("driver"), item.get("serial"),
                        {"endpoint": address, "path": path, "subtype": item.get("subtype")}, (path,)))
            break
    return Probe("gpsd:"+address, "available" if found else "no-devices", found)


def parse_tpv(value, device, sky=None, *, receivedAt=None):
    received = receivedAt or datetime.now(UTC)
    mode = value.get("mode", 0)
    if type(mode) is not int or mode not in (0, 1, 2, 3):
        raise ValueError("Invalid gpsd mode")
    epoch = timestamp(value["time"]) if value.get("time") else received
    # Coordinates without receiver time cannot be assumed current: retain a no-fix event.
    valid = mode >= 2 and "lat" in value and "lon" in value and bool(value.get("time"))
    telemetry = {k: value[k] for k in ("mode", "status", "speed", "track", "climb", "epx", "epy", "epv", "eph", "ept") if k in value}
    error = vertical = altitude = None
    datum = None
    if "eph" in value:
        error = finite(value["eph"], "eph", minimum=0)
    elif "epx" in value and "epy" in value:
        error = math.hypot(finite(value["epx"], "epx", minimum=0), finite(value["epy"], "epy", minimum=0))
    if mode == 3:
        for key, reference in (("altHAE", "ellipsoid"), ("altMSL", "mean-sea-level"), ("alt", "unknown")):
            if key in value:
                altitude, datum = finite(value[key], key), reference
                break
        if "epv" in value:
            vertical = finite(value["epv"], "epv", minimum=0)
    locked = visible = None
    if sky:
        satellites = sky.get("satellites")
        if isinstance(satellites, list):
            visible = len(satellites)
            locked = sum(s.get("used") is True for s in satellites if isinstance(s, dict))
        else:
            locked = sky.get("uSat")
            visible = sky.get("nSat")
        telemetry.update({k: sky[k] for k in ("hdop", "vdop", "pdop") if k in sky})
    estimate = geographic(value["lat"], value["lon"], altitude, error, epoch, device.id,
        uncertaintyMethod="gpsd-reported-error-confidence-unspecified" if error is not None else "unknown",
        verticalError=vertical, altitudeDatum=datum, provenance={"provider": "gpsd", "path": device.options["path"]}) if valid else None
    return Reading(device.id, epoch, received, estimate, "none" if not valid else "3d" if mode == 3 else "2d",
                   device.model, device.serialNumber, locked, visible, value.get("time"), telemetry)


def poll(device, config):
    watch = {"enable": True, "json": True, "device": device.options["path"]}
    sky, skyAt, result = None, 0.0, []
    for value in messages(device.options["endpoint"], "?WATCH="+json.dumps(watch)+";\n", config.readSeconds):
        if value.get("device") != device.options["path"]:
            continue
        if value.get("class") == "SKY":
            sky, skyAt = value, time.monotonic()
        elif value.get("class") == "TPV":
            try:
                result.append(parse_tpv(value, device, sky if time.monotonic()-skyAt < 2 else None))
            except (ValueError, TypeError, KeyError):
                continue
    return result
