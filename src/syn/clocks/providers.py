"""Local clock adapters. Hardware and daemon operations are strictly read-only.

RTC Class drivers include SPI/I2C chips; Syn never writes registers or probes
unknown bus addresses. A PHC's nanosecond interface is not proof of UTC accuracy.
"""
from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import struct
import subprocess
import time

from syn.positions.types import timestamp
from .config import number
from .types import ClockReading, paired_time


def datetime_ns(value):
    delta = timestamp(value)-datetime(1970,1,1,tzinfo=UTC)
    return (delta.days*86400+delta.seconds)*10**9+delta.microseconds*1000


def from_position(reading, config):
    """Reuse the existing receiver reader, rather than open its serial port twice."""
    wall, mono, _ = paired_time()
    received_ns = datetime_ns(reading.receivedAt)
    age_ns = wall-received_ns
    valid = reading.fix in {"2d", "3d"} and reading.timecode is not None
    valid = valid and 0 <= age_ns <= int(config.maxAgeSeconds*1e9)
    metadata = {"deviceId": reading.deviceId, "model": reading.model,
        "serialNumber": reading.serialNumber, "satellitesLocked": reading.satellitesLocked,
        "timecode": reading.timecode, "positionObservedAt": reading.observedAt.isoformat(),
        "requiresStableFix": True, "fix": reading.fix,
        "uncertaintyMethod": "receiver-time-plus-configured-transport-budget",
        "utcValidation": "receiver-asserted; leap-second-validity-not-independently-proven"}
    if not valid:
        return ClockReading("gps:"+reading.deviceId, "gps", wall, mono,
                            valid=False, reason="no-fresh-timed-gps-fix", metadata=metadata)
    reference_ns = datetime_ns(reading.timecode)
    metadata["referenceNs"] = reference_ns
    error = config.gpsTransportErrorMs
    if error is not None:
        reported = reading.telemetry.get("ept")
        error += number(reported, "GPS ept", 0)*1000 if reported is not None else 1000.0
    # Offset at receipt is carried to the current sample pair. Its age grows error.
    if error is not None:
        error += age_ns/1e9*config.holdoverPpm/1000
    return ClockReading("gps:"+reading.deviceId, "gps", wall, mono,
        (reference_ns-received_ns)/1e6, error, 1.0, metadata=metadata)


def parse_chrony(output, wall, mono, latency_ms=0.0):
    fields = dict(line.split(":", 1) for line in output.splitlines() if ":" in line)
    fields = {k.strip(): v.strip() for k,v in fields.items()}
    reference = fields.get("Reference ID", "")
    if (fields.get("Leap status") != "Normal" or reference.startswith(("7F7F0101", "127.127.1.1"))
            or not 1 <= int(fields.get("Stratum", "0")) <= 15):
        raise ValueError("chrony-not-externally-synchronised")
    match = re.fullmatch(r"([0-9.eE+-]+) seconds (slow|fast) of NTP time", fields.get("System time", ""))
    if not match:
        raise ValueError("invalid-chrony-system-time")
    offset = number(float(match[1]), "chrony correction")*1000*(1 if match[2] == "slow" else -1)
    def seconds(name):
        return number(float(fields[name].split()[0]), name)*1000
    error = seconds("Root dispersion")+seconds("Root delay")/2+latency_ms
    return ClockReading("chrony:local", "chrony", wall, mono, offset, error,
        metadata={"reference": reference[:256], "stratum": int(fields["Stratum"]),
                  "leapStatus": fields["Leap status"], "reportedRmsMs": seconds("RMS offset"),
                  "uncertaintyMethod": "chrony-root-distance-plus-command-latency"})


def chrony(config):
    before = time.monotonic_ns()
    result = subprocess.run(["chronyc", "-n", "tracking"], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=config.timeoutSeconds, check=True,
        env={**os.environ, "LC_ALL": "C"})
    wall, mono, _ = paired_time()
    if len(result.stdout) > 16384:
        raise ValueError("oversized-chrony-response")
    return parse_chrony(result.stdout, wall, mono, (mono-before)/1e6)


def parse_gpsd_time(value, address, config):
    wall, mono, bracket = paired_time()
    cls = value.get("class")
    if cls not in {"PPS", "TOFF"}:
        raise ValueError("not-a-gpsd-timing-message")
    numbers = [value[k] for k in ("real_sec", "real_nsec", "clock_sec", "clock_nsec")]
    if any(type(x) is not int for x in numbers) or any(not 0 <= numbers[i] < 10**9 for i in (1,3)):
        raise ValueError("invalid-gpsd-time-pair")
    reference_ns, clock_ns = numbers[0]*10**9+numbers[1], numbers[2]*10**9+numbers[3]
    age = (wall-clock_ns)/1e9
    if not 0 <= age <= min(5, config.maxAgeSeconds):
        raise ValueError("stale-gpsd-time-pair")
    precision = value.get("precision")
    resolution = 2.0**precision*1000 if type(precision) is int and -63 <= precision <= 10 else None
    # PPS resolution alone does not establish UTC accuracy. The transport/error
    # budget is deliberately retained unless a calibrated producer uses observe().
    error = config.gpsTransportErrorMs
    if error is not None:
        error += bracket+age*config.holdoverPpm/1000
    device = str(value.get("device", "unknown"))[:128]
    return ClockReading("gpsd:"+address+":"+device+":"+cls, "gpsd-pps" if cls == "PPS" else "gps",
        wall, mono, (reference_ns-clock_ns)/1e6, error, resolution,
        metadata={"device": device, "messageClass": cls, "referenceNs": reference_ns,
                  "pairedSystemNs": clock_ns, "ageSeconds": age,
                  "uncertaintyMethod": "gpsd-paired-time-plus-configured-error-budget"})


def gpsd(address, config):
    from syn.positions.providers.gpsd import messages
    count = 0
    for value in messages(address, '?WATCH={"enable":true,"json":true,"pps":true};\n', config.timeoutSeconds):
        if value.get("class") in {"PPS", "TOFF"}:
            try:
                reading = parse_gpsd_time(value, address, config)
            except (ValueError, TypeError, KeyError) as error:
                reading = ClockReading.failure("gpsd:"+address, "gpsd-pps", type(error).__name__)
            count += 1
            yield reading
    if not count:
        yield ClockReading.failure("gpsd:"+address, "gpsd-pps", "no-timing-messages")


def device_fd(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISCHR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("clock-device-is-not-a-character-device")
    return fd


def pmc_properties(path, timeout):
    # Caller explicitly binds this management endpoint to one PHC in configuration.
    result = subprocess.run(["pmc", "-u", "-b", "0", "-s", path,
        "GET TIME_PROPERTIES_DATA_SET", "GET TIME_STATUS_NP"], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, check=True, timeout=timeout,
        env={**os.environ, "LC_ALL": "C"})
    if len(result.stdout) > 16384:
        raise ValueError("oversized-pmc-response")
    fields = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            fields[parts[0]] = parts[1]
    if fields.get("gmPresent") not in {"true", "1"} or fields.get("timeTraceable") != "1":
        raise ValueError("ptp-grandmaster-not-traceable")
    return fields


def ptp(policy, config):
    path, scale = policy["path"], policy.get("timescale", "unknown")
    offset = policy.get("taiUtcOffsetSeconds")
    properties = {}
    if policy.get("pmcSocket"):
        properties = pmc_properties(policy["pmcSocket"], config.timeoutSeconds)
        if properties.get("ptpTimescale") == "1":
            scale = "TAI"
            if properties.get("currentUtcOffsetValid") != "1":
                raise ValueError("ptp-utc-offset-unknown")
            offset = int(properties["currentUtcOffset"])
    fd = device_fd(path)
    try:
        before, m1, _ = paired_time()
        reference = time.clock_gettime_ns(((~fd) << 3) | 3)
        after, m2, _ = paired_time()
    finally:
        os.close(fd)
    if abs((after-before)-(m2-m1))/1e6 > config.stepThresholdMs:
        raise ValueError("host-clock-stepped-during-phc-read")
    system, mono = (before+after)//2, (m1+m2)//2
    known = scale == "UTC" or (scale == "TAI" and offset is not None)
    if scale == "TAI" and offset is not None:
        reference -= offset*10**9
    error = policy.get("maxErrorMs") if known else None
    if error is not None:
        error += (after-before)/2e6
    return ClockReading("ptp:"+path, "ptp", system, mono,
        (reference-system)/1e6 if known else None, error, 0.000001,
        metadata={"path": path, "timescale": scale, "taiUtcOffsetSeconds": offset,
            "management": properties, "rawHardwareNs": reference,
            "uncertaintyMethod": "operator-specified-phc-error-plus-read-bracket"})


def rtc(policy, config):
    import fcntl
    if platform.machine().lower() not in {"x86_64", "amd64", "i386", "i686", "aarch64", "arm64", "armv7l", "armv6l", "riscv64"}:
        raise ValueError("unsupported-rtc-ioctl-architecture")
    path = policy["path"]
    fd = device_fd(path)
    voltage = None
    try:
        before, m1, _ = paired_time()
        values = bytearray(36)  # struct rtc_time: nine native C int fields.
        fcntl.ioctl(fd, 0x80247009, values, True)  # RTC_RD_TIME (asm-generic ABI)
        after, m2, _ = paired_time()
        try:
            flags = bytearray(4)
            fcntl.ioctl(fd, 0x80047013, flags, True)  # RTC_VL_READ, when supported
            voltage = struct.unpack("=I", flags)[0]
        except OSError:
            pass
    finally:
        os.close(fd)
    if abs((after-before)-(m2-m1))/1e6 > config.stepThresholdMs:
        raise ValueError("host-clock-stepped-during-rtc-read")
    sec, minute, hour, day, month, year, _, _, _ = struct.unpack("=9i", values)
    epoch = datetime(year+1900, month+1, day, hour, minute, sec, tzinfo=UTC)
    reference = datetime_ns(epoch)+500_000_000  # Centre of the one-second RTC bin.
    wall, mono = (before+after)//2, (m1+m2)//2
    valid = voltage is None or not voltage & 1
    known = policy.get("timescale") == "UTC"
    error = policy.get("maxErrorMs") if known and valid else None
    # RTC calibration must have an epoch; an unknown date cannot be precise forever.
    calibration = policy.get("calibratedAt")
    if error is not None and calibration is not None and reference/1e9 >= calibration:
        age = reference/1e9-calibration
        error += 500+(after-before)/2e6+age*policy.get("driftPpm", config.holdoverPpm)/1000
    else:
        error = None
    entry = Path("/sys/class/rtc")/Path(path).name
    try:
        name = (entry/"name").read_text().strip()[:128]
        device = str((entry/"device").resolve())
    except OSError:
        name, device = None, None
    bus = "i2c" if device and "i2c" in device else "spi" if device and "spi" in device else "platform"
    return ClockReading("rtc:"+path, "rtc", wall, mono, (reference-wall)/1e6 if known else None,
        error, 1000.0, valid=valid, reason="rtc-voltage-invalid" if not valid else None,
        metadata={"path": path, "model": name, "bus": bus, "devicePath": device,
            "voltageFlags": voltage, "timescale": policy.get("timescale", "unknown"),
            "calendar": epoch.isoformat(), "calibratedAt": calibration,
            "uncertaintyMethod": "dated-rtc-calibration-plus-quantisation-and-holdover"})


def pps(entry):
    wall, mono, _ = paired_time()
    value = (entry/"assert").read_text().strip()
    match = re.fullmatch(r"(\d+)\.(\d{1,9})#(\d+)", value)
    if not match:
        raise ValueError("invalid-pps-assert")
    pulse_ns = int(match[1])*10**9+int(match[2].ljust(9,"0"))
    return ClockReading("pps:"+entry.name, "pps", wall, mono,
        metadata={"pulseSystemNs": pulse_ns, "sequence": int(match[3]), "utcPaired": False},
        reason="unpaired-pulse-does-not-identify-a-utc-second")


def local(config):
    # This observation detects a step even when all external sources are unavailable.
    wall, mono, _ = paired_time()
    yield ClockReading("system:realtime", "system", wall, mono,
        resolutionMs=time.get_clock_info("time").resolution*1000,
        metadata={"role": "observation-only-not-a-utc-reference"})
    if config.chrony and shutil.which("chronyc"):
        try:
            yield chrony(config)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            yield ClockReading.failure("chrony:local", "chrony", type(error).__name__)
    if not config.hardware or platform.system() != "Linux":
        return
    for kind, configured, pattern, reader in (("ptp", config.ptpDevices, "ptp[0-9]*", ptp),
                                              ("rtc", config.rtcDevices, "rtc[0-9]*", rtc)):
        policies = {p["path"]: p for p in configured}
        discovered = {str(p): {"path": str(p)} for p in sorted(Path("/dev").glob(pattern))[:32]}
        for path, policy in {**discovered, **policies}.items():
            try:
                yield reader(policy, config)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                yield ClockReading.failure(kind+":"+path, kind, type(error).__name__)
    for entry in sorted(Path("/sys/class/pps").glob("pps[0-9]*"))[:32]:
        try:
            yield pps(entry)
        except (OSError, ValueError) as error:
            yield ClockReading.failure("pps:"+entry.name, "pps", type(error).__name__)
