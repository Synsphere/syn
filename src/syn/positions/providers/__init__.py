"""Independent probes with explicit coverage/permission diagnostics."""
from .base import Device as Device, Probe


def discover(config):
    from . import gpsd, native, serial, network, ble
    reports, devices, claimed = [], [], []
    def attempt(name, action):
        try:
            result = action()
            report, ports = result if isinstance(result, tuple) else (result, ())
            reports.append(report)
            devices.extend(report.devices)
            claimed.extend(ports)
            for d in report.devices:
                claimed.extend(d.claimedPorts)
        except Exception as error:
            # One inaccessible bus must not suppress other hardware providers.
            reports.append(Probe(name, "unavailable", detail=type(error).__name__))
    for endpoint in config.gpsdHosts:
        attempt("gpsd:"+endpoint, lambda endpoint=endpoint: gpsd.probe(endpoint, config))
    if config.nativeServices:
        attempt("native", lambda: native.probe(config))
    if config.serialProbe:
        attempt("serial", lambda: serial.probe(config, claimed))
    if config.bluetoothLE:
        attempt("ble-ln", lambda: ble.probe(config))
    for endpoint in config.networkNmea:
        try:
            devices.append(network.descriptor(endpoint))
        except ValueError:
            reports.append(Probe("network-nmea", "invalid-configuration"))
    # Two gpsd listener addresses can expose the same underlying physical receiver.
    unique, physical = {}, set()
    for device in devices:
        if device.provider == "gpsd":
            key = (device.options["path"], device.serialNumber)
            if key in physical:
                continue
            physical.add(key)
        unique[device.id] = device
    if len(unique) > config.maxDevices:
        reports.append(Probe("inventory", "truncated", detail=f"{len(unique)} candidates exceed maxDevices={config.maxDevices}"))
    return list(unique.values())[:config.maxDevices], [r.to_dict() for r in reports]


def poll(device, config):
    from . import gpsd, native, serial, network, ble
    if device.provider == "serial":
        return serial.poll(device, config)
    functions = {"gpsd": gpsd.poll, "network-nmea": network.poll, "ble-ln": ble.poll,
                 "modemmanager": native.poll, "windows": native.poll, "core-location": native.poll}
    if device.provider not in functions:
        raise ValueError("Unknown position provider")
    return functions[device.provider](device, config), device.options
