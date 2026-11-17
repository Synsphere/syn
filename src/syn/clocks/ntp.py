"""Bounded read-only SNTP exchange (RFC 5905); not an authenticated NTS client."""
from __future__ import annotations

import socket
import struct
import threading
import time

from .types import ClockReading, paired_time

NTP_EPOCH = 2_208_988_800
ERA = 2**32


def encode_time(unix_ns):
    sec, ns = divmod(unix_ns, 10**9)
    return struct.pack("!II", (sec+NTP_EPOCH) % ERA, (ns << 32)//10**9)


def decode_time(raw, near_ns):
    sec, fraction = struct.unpack("!II", raw)
    near_sec = near_ns//10**9 + NTP_EPOCH
    sec += round((near_sec-sec)/ERA)*ERA
    return (sec-NTP_EPOCH)*10**9 + (fraction*10**9 >> 32)


def decode_reply(packet, request_stamp, t1, t4, m1, m4, source, *, step_threshold_ms=100):
    if len(packet) < 48 or len(packet) > 2048 or len(packet) % 4:
        raise ValueError("invalid-packet-length")
    leap, version, mode = packet[0] >> 6, (packet[0] >> 3) & 7, packet[0] & 7
    if version not in (3,4) or mode != 4:
        raise ValueError("not-an-ntp-server-reply")
    if packet[24:32] != request_stamp:
        raise ValueError("origin-timestamp-mismatch")
    if packet[1] == 0:
        raise ValueError("kiss-of-death-"+packet[12:16].decode("ascii", errors="replace"))
    if packet[1] > 15 or leap == 3:
        raise ValueError("unsynchronised-server")
    if packet[32:40] == bytes(8) or packet[40:48] == bytes(8):
        raise ValueError("zero-server-timestamp")
    t2 = decode_time(packet[32:40], t1)
    t3 = decode_time(packet[40:48], t1)
    if m4 < m1 or abs((t4-t1)-(m4-m1))/1e6 > step_threshold_ms:
        raise ValueError("host-clock-stepped-during-exchange")
    delay_ns = (t4-t1)-(t3-t2)
    if t3 < t2 or delay_ns < -1_000_000:
        raise ValueError("inconsistent-server-timestamps")
    delay_ms = max(0, delay_ns)/1e6
    offset_ms = ((t2-t1)+(t3-t4))/2e6
    root_delay = max(0, struct.unpack("!i", packet[4:8])[0]/65536)*1000
    dispersion = struct.unpack("!I", packet[8:12])[0]/65536*1000
    precision = 2.0**struct.unpack("!b", packet[3:4])[0]*1000
    uncertainty = delay_ms/2+root_delay/2+dispersion+precision
    return ClockReading(source, "ntp", t4, m4, offset_ms, uncertainty, precision,
        metadata={"roundTripMs": delay_ms, "rootDelayMs": root_delay, "rootDispersionMs": dispersion,
                  "stratum": packet[1], "leapIndicator": leap, "version": version,
                  "authenticated": False, "uncertaintyMethod": "ntp-root-distance-plus-path-asymmetry"})


def addresses(host, port, timeout):
    """Do not allow an OS resolver stall to block a scheduled job indefinitely."""
    done, result = threading.Event(), []
    def resolve():
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM))
        except OSError as error:
            result.append(error)
        finally:
            done.set()
    threading.Thread(target=resolve, name="syn-clock-dns", daemon=True).start()
    if not done.wait(timeout):
        raise TimeoutError("ntp-dns-timeout")
    if isinstance(result[0], Exception):
        raise result[0]
    return result[0]


def poll(host, timeout=3.0, *, port=123, step_threshold_ms=100):
    source = "ntp:"+host+(":"+str(port) if port != 123 else "")
    deadline = time.monotonic()+timeout
    candidates = addresses(host, port, timeout)
    last_error = TimeoutError("ntp-timeout")
    for family, kind, protocol, _, address in candidates[:8]:
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            break
        try:
            with socket.socket(family, kind, protocol) as sock:
                sock.settimeout(remaining)
                # Connected UDP filters other peers before protocol validation.
                sock.connect(address)
                t1, m1, _ = paired_time()
                stamp = encode_time(t1)
                request = bytearray(48)
                request[0] = 0x23
                request[40:48] = stamp
                sock.send(request)
                packet = sock.recv(2049)
                t4, m4, _ = paired_time()
                return decode_reply(packet, stamp, t1, t4, m1, m4, source,
                                    step_threshold_ms=step_threshold_ms)
        except OSError as error:
            last_error = error
    raise last_error
