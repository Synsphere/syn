"""Strict NMEA-0183 parser. No commands are sent to unidentified serial devices.

Each GGA/RMC sentence is its own observed sample. GSA/GST augmentation has a
bounded age and GST must have the same UTC epoch. No old altitude is carried
into a new 2D observation. RMC dates take precedence over receive-date inference.
"""
from __future__ import annotations
from datetime import UTC, datetime, timedelta
import math
import re
from ..types import Reading, geographic, timestamp


def sentence(raw: bytes | str) -> list[str]:
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw).decode("ascii", errors="strict")
    if not isinstance(raw, str):
        raise ValueError("NMEA input must be ASCII text or bytes")
    raw = raw.strip()
    if not 9 <= len(raw) <= 1024 or not re.fullmatch(r"\$[A-Z0-9]{5},[^*$\r\n]*\*[0-9A-Fa-f]{2}", raw):
        raise ValueError("Malformed NMEA sentence")
    body, checksum = raw[1:].split("*")
    result = 0
    for char in body:
        result ^= ord(char)
    if result != int(checksum, 16):
        raise ValueError("Invalid NMEA checksum")
    return body.split(",")


def coordinate(value: str, direction: str, latitude: bool) -> float:
    if direction not in (("N", "S") if latitude else ("E", "W")):
        raise ValueError("Invalid NMEA hemisphere")
    digits = 2 if latitude else 3
    if not re.fullmatch(r"\d{%d}\d{2}(?:\.\d+)?" % digits, value):
        raise ValueError("Invalid NMEA coordinate")
    degrees, minutes = int(value[:digits]), float(value[digits:])
    if not 0 <= minutes < 60:
        raise ValueError("Invalid NMEA arcminutes")
    output = degrees + minutes/60
    if output > (90 if latitude else 180):
        raise ValueError("NMEA coordinate outside globe")
    return -output if direction in ("S", "W") else output


def optional_number(value: str) -> float | None:
    if not value:
        return None
    x = float(value)
    if not math.isfinite(x):
        raise ValueError("Non-finite NMEA number")
    return x


class NmeaParser:
    def __init__(self, deviceId: str, *, model=None, serialNumber=None, uereMeters=5.0):
        self.deviceId, self.model, self.serialNumber = deviceId, model, serialNumber
        self.uereMeters = uereMeters
        self.date = None
        self.dateReceived = None
        self.gst = None
        self.gsa = None

    def epoch(self, timecode: str, received: datetime, datecode: str | None = None):
        if not re.fullmatch(r"\d{6}(?:\.\d+)?", timecode):
            raise ValueError("Missing UTC time")
        hours, minutes, seconds = int(timecode[:2]), int(timecode[2:4]), float(timecode[4:])
        if not 0 <= hours < 24 or not 0 <= minutes < 60 or not 0 <= seconds < 61:
            raise ValueError("Invalid UTC time")
        inferred = datecode is None
        if datecode:
            if not re.fullmatch(r"\d{6}", datecode):
                raise ValueError("Invalid RMC date")
            # Resolve century nearest receiver date, including recordings after 2079.
            yy = int(datecode[4:])
            century = received.year // 100 * 100
            years = (century+yy-100, century+yy, century+yy+100)
            year = min(years, key=lambda y: abs(y-received.year))
            day = datetime(year, int(datecode[2:4]), int(datecode[:2]), tzinfo=UTC)
            self.date, self.dateReceived = day, received
            inferred = False
        elif self.date is not None and abs((received-self.dateReceived).total_seconds()) < 86400:
            day = self.date
        else:
            day = received.replace(hour=0, minute=0, second=0, microsecond=0)
        value = day + timedelta(hours=hours, minutes=minutes, seconds=seconds)
        if datecode is None:
            value = min((value-timedelta(days=1), value, value+timedelta(days=1)),
                        key=lambda x: abs((x-received).total_seconds()))
        return value, inferred

    def feed(self, raw, *, receivedAt=None) -> Reading | None:
        received = timestamp(receivedAt or datetime.now(UTC))
        p = sentence(raw)
        kind = p[0][-3:]
        if kind == "GSA":
            if len(p) < 18:
                raise ValueError("Truncated GSA")
            self.gsa = (received, p[2], optional_number(p[16]), optional_number(p[17]))
            return None
        if kind == "GST":
            if len(p) < 9:
                raise ValueError("Truncated GST")
            epoch, _ = self.epoch(p[1], received)
            errors = tuple(optional_number(x) for x in p[6:9])
            if any(x is not None and x < 0 for x in errors):
                raise ValueError("Negative GST uncertainty")
            self.gst = (received, epoch, errors)
            return None
        if kind == "ZDA":
            if len(p) < 5:
                raise ValueError("Truncated ZDA")
            self.date = datetime(int(p[4]), int(p[3]), int(p[2]), tzinfo=UTC)
            self.dateReceived = received
            return None
        if kind not in {"GGA", "RMC"}:
            return None
        if len(p) < (15 if kind == "GGA" else 10):
            raise ValueError("Truncated position sentence")
        epoch, inferred = self.epoch(p[1], received, p[9] if kind == "RMC" and p[9] else None)
        telemetry = {"sentence": kind, "talker": p[0][:2], "dateInferred": inferred}
        altitude = error = vertical = satellites = None
        datum = None
        method = "unknown"
        if kind == "GGA":
            quality = int(p[6])
            telemetry["quality"] = quality
            valid = quality in {1, 2, 4, 5}  # reject dead reckoning, simulation, manual, no fix
            satellites = int(p[7]) if p[7] else None
            dop = optional_number(p[8])
            if dop is not None and dop < 0:
                raise ValueError("Negative HDOP")
            telemetry["hdop"] = dop
            if valid:
                latitude, longitude = coordinate(p[2], p[3], True), coordinate(p[4], p[5], False)
                if p[9] and p[10] == "M":
                    altitude = optional_number(p[9])
                    datum = "mean-sea-level"
                if dop is not None and dop > 0:
                    error, method = dop*self.uereMeters, "hdop-times-configured-uere"
                if p[11] and p[12] == "M":
                    telemetry["geoidSeparationMeters"] = optional_number(p[11])
        else:
            valid = p[2] == "A" and (len(p) <= 12 or p[12] not in {"E", "M", "S", "N"})
            if valid:
                latitude, longitude = coordinate(p[3], p[4], True), coordinate(p[5], p[6], False)
                telemetry.update(speedKnots=optional_number(p[7]), courseDegrees=optional_number(p[8]))
        if self.gsa and abs((received-self.gsa[0]).total_seconds()) <= 2:
            if self.gsa[1] == "1":
                valid = False
            if self.gsa[1] == "2":
                altitude = datum = None
            if error is None and self.gsa[2] and self.gsa[2] > 0:
                error, method = self.gsa[2]*self.uereMeters, "hdop-times-configured-uere"
        if self.gst and abs((received-self.gst[0]).total_seconds()) <= 2 and abs((epoch-self.gst[1]).total_seconds()) < .01:
            laterr, lonerr, vertical = self.gst[2]
            if laterr is not None and lonerr is not None:
                error, method = math.hypot(laterr, lonerr), "gst-rms-horizontal-sigma"
        telemetry["uereMeters"] = self.uereMeters if method == "hdop-times-configured-uere" else None
        estimate = geographic(latitude, longitude, altitude, error, epoch, self.deviceId,
                              uncertaintyMethod=method, verticalError=vertical if altitude is not None else None,
                              altitudeDatum=datum, provenance={"provider": "nmea", **telemetry}) if valid else None
        return Reading(self.deviceId, epoch, received, estimate,
                       "none" if not valid else "3d" if altitude is not None else "2d",
                       self.model, self.serialNumber, satellitesLocked=satellites,
                       timecode=epoch.isoformat(), telemetry=telemetry)
