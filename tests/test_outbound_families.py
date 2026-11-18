"""Host route detection without relying on this CI runner's IP connectivity."""
import importlib.util
from pathlib import Path
import pytest

source = Path(__file__).resolve().parents[1] / 'src' / 'syn' / 'outbound_families.py'
spec = importlib.util.spec_from_file_location('outbound_families', source)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_ipv6_route_cache_lifetime_and_recovery():
    clock = [0.0]
    probes = [False, True]
    instance = mod.OutboundFamilies(lambda: probes.pop(0), ttl_seconds=60,
                                    clock=lambda: clock[0])
    assert instance.ipv6() is False
    clock[0] = 59.9
    assert instance.ipv6() is False
    assert probes == [True]
    clock[0] = 60.0
    assert instance.ipv6() is True
    assert instance.status()['outboundIpv6Route'] is True


def test_ipv6_probe_failure_degrades_only_ipv6():
    def failed():
        raise OSError('no route')
    instance = mod.OutboundFamilies(failed)
    assert instance.status()['outboundIpv6Route'] is False


@pytest.mark.parametrize('ttl', [0, -1, 3601])
def test_ttl_bounded(ttl):
    with pytest.raises(ValueError):
        mod.OutboundFamilies(ttl_seconds=ttl)


def test_wsl_kernel_detection(monkeypatch, tmp_path):
    monkeypatch.delenv('WSL_INTEROP', raising=False)
    monkeypatch.delenv('WSL_DISTRO_NAME', raising=False)
    fake = tmp_path / 'osrelease'
    fake.write_text('5.15.90.1-microsoft-standard-WSL2')
    monkeypatch.setattr(mod, 'Path', lambda _: fake)
    assert mod.in_wsl()
