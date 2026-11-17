"""Explicit TCP NMEA receivers and UDP listeners. Never scans networks."""
import socket
import time
from urllib.parse import urlsplit
from .base import Device
from .gpsd import endpoint
from .nmea import NmeaParser


def descriptor(url):
    value = urlsplit(url)
    if value.scheme not in {"tcp", "udp"} or value.path or value.query or value.fragment:
        raise ValueError("NMEA URL must be tcp://numeric-IP:port or udp://local-IP:port")
    endpoint(value.netloc)
    return Device("network-nmea", url)


def poll(device, config):
    value = urlsplit(device.address)
    address = endpoint(value.netloc)
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    parser = NmeaParser(device.id, uereMeters=config.uereMeters)
    output, pending, total = [], bytearray(), 0
    deadline = time.monotonic()+config.readSeconds
    with socket.socket(family, socket.SOCK_DGRAM if value.scheme == "udp" else socket.SOCK_STREAM) as stream:
        stream.settimeout(config.readSeconds)
        if value.scheme == "udp":
            stream.bind(address)
        else:
            stream.connect(address)
        while time.monotonic() < deadline and total < 262144 and len(output) < 128:
            stream.settimeout(max(.01, deadline-time.monotonic()))
            try:
                raw = stream.recv(8192)
            except TimeoutError:
                break
            except ConnectionResetError:
                if output:
                    break
                raise
            if not raw:
                break
            total += len(raw)
            pending.extend(raw)
            if value.scheme == "udp" and not pending.endswith(b"\n"):
                pending.extend(b"\n")
            while b"\n" in pending:
                line, _, pending = pending.partition(b"\n")
                try:
                    reading = parser.feed(line)
                    if reading:
                        output.append(reading)
                except (ValueError, UnicodeError, IndexError, OverflowError):
                    continue
            if len(pending) > 1024:
                pending.clear()
    return output
