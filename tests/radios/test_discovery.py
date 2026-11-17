from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from syn.radios.config import RadioConfig
from syn.radios.discovery import RadioDiscovery
from syn.radios.providers import (LinuxWifi, LinuxBluetooth, LinuxBusRadios,
                                  LinuxUsbRadios, Probe, Soapy)
from syn.radios.types import RadioDevice, digest


class Provider:
    def __init__(self, name="test", devices=(), error=False):
        self.name = name
        self.devices = devices
        self.error = error

    def scan(self):
        if self.error:
            raise PermissionError("private information must not be logged")
        return list(self.devices)


def item(**options):
    return RadioDevice("test", "test0", "wifi", "wlan0", "netlink", **options)


def test_unknown_capabilities_are_not_false():
    radio = item()
    assert radio.canTransmit is None and radio.canReceive is None
    assert radio.to_dict()["deviceId"] == radio.id
    assert RadioDevice.from_dict(radio.to_dict()) == radio
    assert radio.id == RadioDevice.from_dict(radio.to_dict()).id


def test_normalize_capability_evidence_and_metadata():
    radio = item(features=("ap", "ap", "mesh"), evidence=("sysfs", "sysfs"),
                 details={"model": "WiFi\nModel"})
    assert radio.features == ("ap", "mesh")
    assert radio.details["model"] == "WiFiModel"
    assert len(radio.evidence) == 1


def test_invalid_descriptor_and_config():
    with pytest.raises(ValueError):
        RadioConfig(rescanSeconds=5)
    with pytest.raises(ValueError):
        RadioConfig.from_dict({"unknown": 1})
    with pytest.raises(TypeError):
        item(canTransmit="yes")
    with pytest.raises(ValueError):
        item(bandsMHz=((100, 50),))


def test_provider_failure_is_bounded_and_redacted():
    results = RadioDiscovery(providers=[Provider(error=True)]).scan()
    assert results[0].status == "error"
    assert results[0].errorType == "PermissionError"
    assert "private" not in str(results)


def test_duplicate_providers_and_devices_fail_closed():
    with pytest.raises(ValueError):
        RadioDiscovery(providers=[Provider(), Provider()])
    results = RadioDiscovery(providers=[Provider(devices=(item(), item()))]).scan()
    assert results[0].status == "error"
    assert results[0].errorType == "ValueError"


def test_max_devices_does_not_report_false_absence():
    devices = [RadioDevice("test", f"dev{i}", "sdr", "radio", "usb") for i in range(3)]
    results = RadioDiscovery(RadioConfig(maxDevices=2), [Provider(devices=devices)]).scan()
    assert results[0].status == "error"


def test_wifi_phy_interpretation_80211ah():
    raw = """Wiphy phy0
      Supported interface modes:
        * managed
        * AP
        * monitor
      Band 1:
        * 915 MHz [1]
      S1G support:
        HE Iftypes:
"""
    result = LinuxWifi.parse("phy0", raw)
    assert result.category == "halow"
    assert result.canReceive and result.canTransmit
    assert "802.11ah" in result.features
    assert (915.0, 915.0) in result.bandsMHz
    assert "mode:ap" in result.features


def test_wifi_uncertain_phy_when_no_iw(monkeypatch, tmp_path):
    import syn.radios.providers as providers
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    (tmp_path / "class/ieee80211/phy0").mkdir(parents=True)
    probe = Probe(RadioConfig(useCommands=False), sysroot=tmp_path)
    result = LinuxWifi(probe).scan()[0]
    assert result.category == "wifi" and result.canTransmit is None
    assert result.support == "identified"


def test_bluez_reported_le_and_classic():
    result = LinuxBluetooth.parse("hci0", "supported settings: powered br/edr le advertising secure-conn")
    assert "ble" in result.features
    assert "bluetooth-classic" in result.features
    assert "ble-advertising" in result.features


def test_linux_spi_lora_and_i2c_fm(monkeypatch, tmp_path):
    import syn.radios.providers as providers
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    spi = tmp_path / "bus/spi/devices/spi0.0"
    i2c = tmp_path / "bus/i2c/devices/1-0010"
    spi.mkdir(parents=True)
    i2c.mkdir(parents=True)
    (spi / "modalias").write_text("spi:sx1302")
    (i2c / "modalias").write_text("i2c:rda5807")
    found = LinuxBusRadios(Probe(RadioConfig(), sysroot=tmp_path)).scan()
    assert {f.category for f in found} == {"lora-concentrator", "fm"}
    assert all(f.support == "identified" and f.canTransmit is None for f in found)


def test_spi_generic_must_not_identify_random_chip(monkeypatch, tmp_path):
    import syn.radios.providers as providers
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    node = tmp_path / "bus/spi/devices/spi0.0"
    node.mkdir(parents=True)
    (node / "modalias").write_text("spidev:unknown")
    assert not LinuxBusRadios(Probe(RadioConfig(), sysroot=tmp_path)).scan()


def test_usb_known_sdr_and_unknown_bridge(monkeypatch, tmp_path):
    import syn.radios.providers as providers
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    root = tmp_path / "bus/usb/devices"
    for dev, vid, pid in (("1-1", "0bda", "2838"), ("1-2", "0403", "6001")):
        path = root / dev
        path.mkdir(parents=True)
        (path / "idVendor").write_text(vid)
        (path / "idProduct").write_text(pid)
    found = LinuxUsbRadios(Probe(RadioConfig(), sysroot=tmp_path)).scan()
    assert len(found) == 1
    assert found[0].category == "sdr"
    assert found[0].canReceive is True and found[0].canTransmit is False


def test_soapy_sdr_parser_and_serial_redaction():
    raw = """######################################################
Found device 0
  driver = rtlsdr
  label = Generic RTL2832U
  serial = 123456
Found device 1
  driver = hackrf
  label = HackRF One
  serial = 234567
"""
    found = Soapy.parse(raw)
    assert len(found) == 2
    assert found[0].canTransmit is False
    assert found[1].canTransmit is True
    assert "123456" not in str([x.to_dict() for x in found])
    assert found[0].details["serialHash"] == digest("123456")


def test_command_timeout_and_missing_utility(monkeypatch):
    import syn.radios.providers as providers
    monkeypatch.setattr(providers.shutil, "which", lambda cmd: "/bin/mock")
    def failing(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)
    assert Probe(RadioConfig(), executor=failing).command("iw", "dev") is None


def test_command_is_bounded(monkeypatch):
    import syn.radios.providers as providers
    monkeypatch.setattr(providers.shutil, "which", lambda cmd: "/bin/mock")
    seen = []
    def run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="hi")
    assert Probe(RadioConfig(), executor=run).command("iw", "phy") == "hi"
    assert seen[0][1]["timeout"] == RadioConfig().probeTimeoutSeconds
    assert seen[0][1]["stdin"] == subprocess.DEVNULL


def test_serial_radio_candidate_does_not_expose_serial_number(monkeypatch, tmp_path):
    import syn.radios.providers as providers
    from syn.radios.providers import LinuxSerialRadios
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    root = tmp_path / "serial/by-id"
    root.mkdir(parents=True)
    (root / "usb-lora-controller-serial123456").touch()
    devices = LinuxSerialRadios(Probe(RadioConfig(), devroot=tmp_path)).scan()
    assert len(devices) == 1 and devices[0].category == "lora"
    assert "serial123456" not in str(devices[0].to_dict())


def test_soapy_network_capable_enumerator_disabled_by_default():
    assert RadioConfig().useSoapySDR is False


def test_windows_wifi_reported_modes(monkeypatch):
    import json
    import syn.radios.providers as providers
    from syn.radios.providers import WindowsRadio
    monkeypatch.setattr(providers.os, "name", "nt", raising=False)
    class FakeProbe:
        def command(self, *args):
            if args[0] == "netsh":
                return "Radio types supported : 802.11n 802.11ax 802.11ah"
            return json.dumps([{"Class": "Net", "FriendlyName": "Wireless Adapter",
                                "InstanceId": "PCI\\VEN_1234", "Status": "OK"}])
    found = WindowsRadio(FakeProbe()).scan()
    assert found[0].category == "halow"
    assert "802.11ah" in found[0].features
    assert found[0].canTransmit is None


def test_linux_ieee_802154_and_nfc_classes(monkeypatch, tmp_path):
    import syn.radios.providers as providers
    from syn.radios.providers import LinuxRadioClasses
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    (tmp_path / "class/ieee802154/phy0").mkdir(parents=True)
    (tmp_path / "class/nfc/nfc0").mkdir(parents=True)
    found = LinuxRadioClasses(Probe(RadioConfig(), sysroot=tmp_path)).scan()
    assert {r.category for r in found} == {"802.15.4", "nfc"}
    assert all(r.canTransmit is None for r in found)
