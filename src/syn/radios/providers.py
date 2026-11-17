"""Bounded passive RF inventory probes. Never transmit, tune or scan RF networks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Protocol

from .catalogue import USB_RADIOS, capability, chip_from_signature
from .config import RadioConfig
from .types import RadioDevice, clean, digest


class RadioProvider(Protocol):
    name: str

    def scan(self) -> list[RadioDevice]:
        ...


class Probe:
    def __init__(self, config: RadioConfig, *, sysroot: Path = Path("/sys"),
                 executor: Callable | None = None, devroot: Path = Path("/dev")):
        self.config = config
        self.sysroot = sysroot
        self.devroot = devroot
        self.executor = executor or subprocess.run

    def command(self, *args: str, timeout: float | None = None) -> str | None:
        if not self.config.useCommands or not shutil.which(args[0]):
            return None
        try:
            result = self.executor(list(args), capture_output=True, text=True,
                                   timeout=timeout or self.config.probeTimeoutSeconds,
                                   stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout[:65536]

    @staticmethod
    def read(path: Path, limit: int = 256) -> str:
        try:
            return clean(path.read_text(errors="replace")[:limit], limit)
        except (OSError, ValueError):
            return ""

    @staticmethod
    def children(path: Path, limit: int = 256) -> list[Path]:
        try:
            return sorted(path.iterdir())[:limit]
        except OSError:
            return []


class LinuxWifi:
    name = "linux-wifi"

    def __init__(self, probe: Probe):
        self.probe = probe

    @staticmethod
    def parse(phy: str, output: str, driver: str | None = None) -> RadioDevice:
        lower = output.casefold()
        halow = bool(re.search(r"\b(?:s1g|802[.]11ah|wi-fi halow)\b", lower))
        bands = sorted({float(v) for v in re.findall(r"\*\s*(\d+(?:\.\d+)?)\s*MHz\b", output, re.I)})
        # A listed channel is not an assertion that every frequency between bands is available.
        if len(bands) > 32:
            bands = [x for x in bands if x < 1000][:12] + [x for x in bands if 1000 <= x < 3000][:8] + [
                x for x in bands if 3000 <= x < 5900][:8] + [x for x in bands if x >= 5900][:4]
        modes = re.findall(r"^\s*\*\s*(managed|AP|monitor|mesh point|P2P-client|P2P-GO|ibss)\s*$", output, re.M)
        features = {"mode:" + mode.lower().replace(" ", "-") for mode in modes}
        for name, evidence in (("802.11n", "HT Capabilities"), ("802.11ac", "VHT Capabilities"),
                               ("802.11ax", "HE Iftypes"), ("802.11be", "EHT Iftypes"),
                               ("802.11ah", "S1G")):
            if evidence.casefold() in lower:
                features.add(name)
        for mhz in (20, 40, 80, 160, 320):
            if re.search(rf"\b{mhz}\s*MHz\b", output, re.I):
                features.add(f"width:{mhz}MHz")
        return RadioDevice(provider="linux-wifi", path=phy, category="halow" if halow else "wifi",
                           name=phy, transport="netlink", driver=driver,
                           canReceive=True, canTransmit=True, features=tuple(features),
                           bandsMHz=tuple((x, x) for x in bands),
                           evidence=("iw phy info",), support="driver")

    def scan(self) -> list[RadioDevice]:
        if platform.system() != "Linux":
            return []
        root = self.probe.sysroot / "class/ieee80211"
        phys = [p.name for p in self.probe.children(root)]
        # If sysfs is hidden in a container but nl80211 is available, use iw's listing.
        if not phys:
            listing = self.probe.command("iw", "dev") or ""
            phys = re.findall(r"^phy#(\d+)\s*$", listing, re.M)
            phys = ["phy" + n for n in phys]
        items = []
        for phy in phys:
            device = root / phy / "device"
            driver = device / "driver"
            name = driver.resolve().name if driver.is_symlink() else None
            info = self.probe.command("iw", "phy", phy, "info")
            if info:
                items.append(self.parse(phy, info, name))
            else:
                # No tool or insufficient privileges: category exists, abilities unknown.
                items.append(RadioDevice("linux-wifi", phy, "wifi", phy, "netlink", driver=name,
                                         evidence=("sysfs:ieee80211",), support="identified"))
        return items


class LinuxBluetooth:
    name = "linux-bluetooth"

    def __init__(self, probe: Probe):
        self.probe = probe

    @staticmethod
    def parse(name: str, output: str) -> RadioDevice:
        lower = output.casefold()
        le = bool(re.search(r"\b(?:le|low energy)\b", lower))
        classic = "br/edr" in lower
        features = []
        for label, expr in (("ble", le), ("bluetooth-classic", classic),
                            ("ble-advertising", "advertising" in lower),
                            ("le-extended-advertising", "extended advertising" in lower),
                            ("bluetooth-mesh", "mesh" in lower),
                            ("secure-connections", "secure-conn" in lower)):
            if expr:
                features.append(label)
        return RadioDevice("linux-bluetooth", name, "bluetooth", name,
                           "hci", canReceive=True, canTransmit=True,
                           features=tuple(features), evidence=("btmgmt info",), support="driver")

    def scan(self) -> list[RadioDevice]:
        if platform.system() != "Linux":
            return []
        adapters = [p.name for p in self.probe.children(self.probe.sysroot / "class/bluetooth")
                    if re.fullmatch(r"hci\d+", p.name)]
        result = []
        for adapter in adapters:
            output = self.probe.command("btmgmt", "--index", adapter[3:], "info")
            if output:
                result.append(self.parse(adapter, output))
            else:
                result.append(RadioDevice("linux-bluetooth", adapter, "bluetooth", adapter, "hci",
                                          evidence=("sysfs:bluetooth",)))
        return result


class LinuxRfkill:
    name = "linux-rfkill"

    def __init__(self, probe: Probe):
        self.probe = probe

    def scan(self) -> list[RadioDevice]:
        if platform.system() != "Linux":
            return []
        result = []
        for entry in self.probe.children(self.probe.sysroot / "class/rfkill"):
            category = self.probe.read(entry / "type").lower()
            if category not in {"wlan", "bluetooth", "wwan", "uwb", "nfc", "gps", "fm"}:
                continue
            # rfkill may refer to the same adapter as the wifi/BT enumerators.
            result.append(RadioDevice("linux-rfkill", entry.name, "wifi" if category == "wlan" else category,
                                      self.probe.read(entry / "name") or entry.name, "rfkill",
                                      evidence=("sysfs:rfkill",), details={
                                          "softBlocked": self.probe.read(entry / "soft"),
                                          "hardBlocked": self.probe.read(entry / "hard")}))
        return result


class LinuxBusRadios:
    name = "linux-bus"

    def __init__(self, probe: Probe):
        self.probe = probe

    def scan(self) -> list[RadioDevice]:
        if platform.system() != "Linux" or not self.probe.config.useSysfs:
            return []
        devices = []
        for bus in ("spi", "i2c"):
            for item in self.probe.children(self.probe.sysroot / "bus" / bus / "devices"):
                modalias = self.probe.read(item / "modalias", 1024)
                compatible = self.probe.read(item / "of_node" / "compatible", 1024).replace("\x00", ",")
                description = f"{item.name} {modalias} {compatible} "
                match = chip_from_signature(description)
                if match is None:
                    continue
                chip, category, mode = match
                driver = item / "driver"
                driver_name = driver.resolve().name if driver.is_symlink() else None
                rx, tx = capability(mode)
                devices.append(RadioDevice("linux-bus", f"{bus}:{item.name}", category,
                                           chip, bus, chipset=chip, driver=driver_name,
                                           canReceive=rx if driver_name else None,
                                           canTransmit=tx if driver_name else None,
                                           evidence=("sysfs:modalias-or-compatible",),
                                           support="driver" if driver_name else "identified",
                                           details={"modalias": modalias[:200]}))
        # The kernel radio tuner class indicates FM receiver functionality; never tune.
        for item in self.probe.children(self.probe.sysroot / "class/video4linux"):
            if not re.fullmatch(r"radio\d+", item.name):
                continue
            driver = item / "device/driver"
            devices.append(RadioDevice("linux-bus", "v4l2:" + item.name, "fm",
                                       self.probe.read(item / "name") or item.name, "v4l2",
                                       driver=driver.resolve().name if driver.is_symlink() else None,
                                       canReceive=True, canTransmit=False,
                                       evidence=("sysfs:v4l2-radio",), support="driver"))
        return devices


class LinuxUsbRadios:
    name = "linux-usb"

    def __init__(self, probe: Probe):
        self.probe = probe

    def scan(self) -> list[RadioDevice]:
        if platform.system() != "Linux" or not self.probe.config.useSysfs:
            return []
        result = []
        for item in self.probe.children(self.probe.sysroot / "bus/usb/devices"):
            vid = self.probe.read(item / "idVendor").lower()
            pid = self.probe.read(item / "idProduct").lower()
            known = USB_RADIOS.get((vid, pid))
            if not known:
                continue
            name, category, mode, chipset = known
            rx, tx = capability(mode)
            result.append(RadioDevice("linux-usb", item.name, category,
                                      self.probe.read(item / "product") or name, "usb",
                                      chipset=chipset, canReceive=rx, canTransmit=tx,
                                      evidence=(f"usb:{vid}:{pid}",),
                                      details={"manufacturer": self.probe.read(item / "manufacturer")}))
        return result


class Soapy:
    name = "soapy-sdr"

    def __init__(self, probe: Probe):
        self.probe = probe

    @staticmethod
    def parse(output: str) -> list[RadioDevice]:
        result = []
        blocks = re.split(r"(?=^\s*Found device\s+\d+\s*$)", output, flags=re.M)
        for index, block in enumerate(blocks):
            if not re.search(r"^\s*Found device\s+\d+", block, re.M):
                continue
            properties = {k.lower(): v.strip() for k, v in re.findall(
                r"^\s*([\w-]+)\s*=\s*(.*?)\s*$", block, re.M)}
            driver = clean(properties.get("driver", ""), 80)
            if not driver:
                continue
            label = clean(properties.get("label", driver), 128)
            serial = properties.get("serial")
            locator = digest([driver, serial]) if serial else digest([driver, label, index])
            receive, transmit = None, None
            # Only two known driver families get a coarse topology claim.
            if driver in {"rtlsdr", "airspy", "sdrplay"}:
                receive, transmit = True, False
            elif driver in {"hackrf", "lime", "bladerf", "plutosdr"}:
                receive, transmit = True, True
            result.append(RadioDevice("soapy-sdr", locator, "sdr", label, "soapy",
                                      driver=driver, canReceive=receive, canTransmit=transmit,
                                      evidence=("SoapySDRUtil --find",), support="driver",
                                      details={"serialHash": digest(serial) if serial else ""}))
        return result

    def scan(self) -> list[RadioDevice]:
        if not self.probe.config.useSoapySDR:
            return []
        output = self.probe.command("SoapySDRUtil", "--find")
        return self.parse(output) if output else []


class LinuxRadioClasses:
    name = "linux-radio-classes"

    def __init__(self, probe: Probe):
        self.probe = probe

    def scan(self) -> list[RadioDevice]:
        if platform.system() != "Linux" or not self.probe.config.useSysfs:
            return []
        result = []
        for sys_class, category, standard in (
            ("ieee802154", "802.15.4", "802.15.4"),
            ("nfc", "nfc", "NFC"),
            ("uwb", "uwb", "ultra-wideband"),
            ("wwan", "cellular", "WWAN"),
        ):
            for entry in self.probe.children(self.probe.sysroot / "class" / sys_class):
                result.append(RadioDevice("linux-radio-classes", sys_class+":"+entry.name,
                    category, entry.name, "kernel-class",
                    features=(standard,), evidence=("sysfs:"+sys_class,),
                    support="identified"))
        return result


class LinuxSerialRadios:
    name = "linux-radio-serial"

    def __init__(self, probe: Probe):
        self.probe = probe

    def scan(self) -> list[RadioDevice]:
        if platform.system() != "Linux" or not self.probe.config.useSysfs:
            return []
        result = []
        # /dev/serial/by-id is informative; no serial port is opened or written.
        for entry in self.probe.children(self.probe.devroot / "serial/by-id"):
            label = entry.name.lower()
            if not any(term in label for term in (
                "lora", "zigbee", "zwave", "z-wave", "radiolib", "sx130", "sx126",
                "cc1101", "nrf24", "rfm69", "rtl-sdr", "hackrf", "airspy", "mesh")):
                continue
            if "zigbee" in label:
                category = "802.15.4"
            elif "zwave" in label or "z-wave" in label:
                category = "z-wave"
            elif "sdr" in label or "hackrf" in label or "airspy" in label:
                category = "sdr"
            elif "mesh" in label:
                category = "mesh-radio"
            else:
                category = "lora" if ("lora" in label or "sx" in label) else "ism"
            result.append(RadioDevice("linux-radio-serial", digest(entry.name), category,
                                      category + " radio candidate", "serial", evidence=("udev:serial-by-id",)))
        return result


class MacRadio:
    name = "macos-radios"

    def __init__(self, probe: Probe):
        self.probe = probe

    def scan(self) -> list[RadioDevice]:
        if platform.system() != "Darwin":
            return []
        output = self.probe.command("system_profiler", "SPAirPortDataType", "SPBluetoothDataType",
                                    "SPUSBDataType", "-json", timeout=max(6, self.probe.config.probeTimeoutSeconds))
        if not output:
            return []
        root = json.loads(output)
        found = []
        # Do NOT traverse association, current network, BT paired devices or
        # nearby networks: those are peers, not interfaces owned by this host.
        for key, category in (("SPAirPortDataType", "wifi"), ("SPBluetoothDataType", "bluetooth")):
            data = root.get(key, [])
            if not isinstance(data, list):
                continue
            for i, item in enumerate(data[:8]):
                if isinstance(item, dict):
                    found.append(RadioDevice("macos-radios", f"{key}:{i}", category,
                        clean(str(item.get("_name") or category)), "system-profiler",
                        evidence=(f"system_profiler:{key}",)))
        # USB inventory is intentionally limited to known radio VID/PID labels.
        def usb_walk(value, index=""):
            if isinstance(value, list):
                for i, v in enumerate(value[:64]):
                    usb_walk(v, index + f"/{i}")
            if not isinstance(value, dict):
                return
            vendor = str(value.get("vendor_id", "")).lower()
            product = str(value.get("product_id", "")).lower()
            vid = re.search(r"0x([0-9a-f]{4})", vendor)
            pid = re.search(r"0x([0-9a-f]{4})", product)
            known = USB_RADIOS.get((vid.group(1), pid.group(1))) if vid and pid else None
            if known:
                found.append(RadioDevice("macos-radios", "usb"+index, "sdr",
                                         known[0], "usb", chipset=known[3],
                                         evidence=("system_profiler:USB-ID",)))
            for k, child in value.items():
                if isinstance(child, (list, dict)) and k == "_items":
                    usb_walk(child, index + "/_items")
        usb_walk(root.get("SPUSBDataType", []))
        return found


class WindowsRadio:
    name = "windows-radios"

    def __init__(self, probe: Probe):
        self.probe = probe

    def scan(self) -> list[RadioDevice]:
        if os.name != "nt":
            return []
        # Enumerate local PnP radio devices only, no Wi-Fi BSS or Bluetooth peer scanning.
        command = ("Get-PnpDevice -PresentOnly | Where-Object { $_.Class -in "
                   "@('Net','Bluetooth','Radio') -or $_.InstanceId -match 'VID_[0-9A-F]{4}' } | "
                   "Select-Object Class,FriendlyName,InstanceId,Status | ConvertTo-Json -Compress")
        output = self.probe.command("powershell", "-NoProfile", "-NonInteractive", "-Command", command)
        if not output:
            return []
        raw = json.loads(output)
        result = []
        wlan = self.probe.command("netsh", "wlan", "show", "drivers") or ""
        reported_modes = tuple(sorted({"802.11" + m.lower() for m in
            re.findall(r"802[.]11\s*([a-z]{1,2})\b", wlan, re.I)}))
        halow_wlan = "802.11ah" in reported_modes or "halow" in wlan.lower()
        for row in raw if isinstance(raw, list) else [raw]:
            if not isinstance(row, dict):
                continue
            category = str(row.get("Class", "")).lower()
            name = clean(str(row.get("FriendlyName") or ""))
            instance = str(row.get("InstanceId") or "")
            usb = re.search(r"VID_([A-Fa-f0-9]{4})&PID_([A-Fa-f0-9]{4})", instance)
            identified = USB_RADIOS.get(tuple(x.lower() for x in usb.groups())) if usb else None
            if identified:
                category = "sdr"
            elif category == "net":
                if not re.search("wireless|wi-?fi|802[.]11|halow", name, re.I):
                    continue
                category = "halow" if halow_wlan or re.search(r"halow|802[.]11ah", name, re.I) else "wifi"
            elif category not in {"bluetooth", "radio"}:
                continue
            result.append(RadioDevice("windows-radios", digest(instance), category,
                                      name or category, "pnp", chipset=identified[3] if identified else None,
                                      features=reported_modes if category in {"wifi", "halow"} else (),
                                      evidence=("Get-PnpDevice", "netsh wlan show drivers") if wlan and category in {"wifi", "halow"} else ("Get-PnpDevice",),
                                      details={"status": str(row.get("Status", ""))}))
        return result


class RadioLibBridge:
    """Opt-in cooperative RadioLib firmware: never interrogate unknown USB serial."""
    name = "radiolib-bridge"

    def __init__(self, probe: Probe):
        self.probe = probe

    def scan(self) -> list[RadioDevice]:
        if not self.probe.config.radioLibPorts:
            return []
        try:
            import serial
        except ImportError:
            return []
        result = []
        for port in self.probe.config.radioLibPorts:
            # The administrator explicitly authorized each serial port.
            try:
                with serial.Serial(port=port, baudrate=115200,
                                   timeout=self.probe.config.probeTimeoutSeconds,
                                   write_timeout=self.probe.config.probeTimeoutSeconds) as device:
                    device.write(b"SYN-RADIOLIB/1?\n")
                    raw = device.readline(2049)
            except (OSError, serial.SerialException):
                continue
            if len(raw) > 2048 or not raw.startswith(b"SYN-RADIOLIB/1 "):
                continue
            try:
                metadata = json.loads(raw[len(b"SYN-RADIOLIB/1 "):])
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(metadata, dict) or metadata.get("library") != "RadioLib":
                continue
            chipset = clean(str(metadata.get("chipset", "unknown")))
            match = chip_from_signature(chipset)
            category = match[1] if match else "ism"
            features = metadata.get("features", [])
            if not isinstance(features, list) or not all(isinstance(x, str) for x in features):
                features = []
            rx, tx = metadata.get("rx"), metadata.get("tx")
            rx = rx if type(rx) is bool else None
            tx = tx if type(tx) is bool else None
            result.append(RadioDevice("radiolib-bridge", digest(os.path.realpath(port)), category,
                                      "RadioLib " + chipset, "serial-bridge", chipset=chipset,
                                      canReceive=rx, canTransmit=tx, features=tuple(features),
                                      evidence=("SYN-RADIOLIB/1 handshake",), support="protocol"))
        return result


def default_providers(config: RadioConfig, *, probe: Probe | None = None) -> tuple[RadioProvider, ...]:
    probe = probe or Probe(config)
    return (LinuxWifi(probe), LinuxBluetooth(probe), LinuxRfkill(probe),
            LinuxBusRadios(probe), LinuxUsbRadios(probe), LinuxRadioClasses(probe),
            LinuxSerialRadios(probe), Soapy(probe),
            MacRadio(probe), WindowsRadio(probe), RadioLibBridge(probe))
