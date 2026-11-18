"""The Windows-only router manager runs against a strict fake IGD on Linux."""
import importlib.util
from pathlib import Path
import pytest

source = Path(__file__).resolve().parents[1] / 'scripts' / 'syn_windows_upnp.py'
spec = importlib.util.spec_from_file_location('syn_windows_upnp', source)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Router:
    lanaddr = '192.168.1.123'
    def __init__(self, *, external='203.0.113.30', existing=None, discovered=1):
        self.existing = existing
        self.external = external
        self.discovered = discovered
        self.deleted = self.added = 0
    def discover(self): return self.discovered
    def selectigd(self): return True
    def externalipaddress(self): return self.external
    def getspecificportmapping(self, port, protocol): return self.existing
    def addportmapping(self, port, protocol, internal, internalport, description, remotehost, lease):
        self.added += 1
        self.existing = (internal, internalport, description, '1', lease)
        return True
    def deleteportmapping(self, port, protocol):
        self.deleted += 1
        self.existing = None
        return True


# Python's global/public predicate correctly rejects documentation ranges.
# A routable address below is only used for a *synthetic* IGD, never contacted.
_PUBLIC = '8.8.8.8'


def test_router_maps_only_to_windows_lan_and_verifies_readback():
    router = Router(external=_PUBLIC)
    report = mod.manage('map', 4001, upnp=router)
    assert report['mappingVerified'] is True
    assert report['destination'] == '192.168.1.123:4001'
    assert router.existing[:3] == ('192.168.1.123', 4001, 'Syn WSL TCP 4001')
    assert mod.manage('inspect', 4001, upnp=router)['owned'] is True
    assert mod.manage('remove', 4001, upnp=router)['removed'] is True
    assert router.deleted == 1


def test_conflict_never_overwritten_or_deleted():
    router = Router(external=_PUBLIC, existing=('192.168.1.18', 4001, 'Other application'))
    for action in ('map', 'remove'):
        with pytest.raises(mod.IngressError, match='different router mapping'):
            mod.manage(action, 4001, upnp=router)
    assert router.added == 0 and router.deleted == 0


def test_wrong_lan_or_cgnat_fails_closed():
    with pytest.raises(mod.IngressError, match='different LAN interface'):
        mod.manage('map', 4001, upnp=Router(external=_PUBLIC), expected_lan='192.168.1.4')
    with pytest.raises(mod.IngressError, match='not public'):
        mod.manage('map', 4001, upnp=Router(external='100.64.10.1'))
    with pytest.raises(mod.IngressError, match='No UPnP'):
        mod.manage('map', 4001, upnp=Router(external=_PUBLIC, discovered=0))


def test_mapping_verification_does_not_accept_description_mismatch():
    router = Router(external=_PUBLIC, existing=('192.168.1.123', 4001, 'Another owner'))
    with pytest.raises(mod.IngressError):
        mod.manage('map', 4001, upnp=router)
    assert router.deleted == router.added == 0


def test_igd_714_is_absence_not_discovery_success():
    router = Router(external=_PUBLIC)
    normal = router.getspecificportmapping
    def absent(port, protocol):
        if router.existing is None:
            raise RuntimeError(714)
        return normal(port, protocol)
    router.getspecificportmapping = absent
    assert mod.manage('map', 4001, upnp=router)['mappingVerified']


def test_igd_unrelated_query_failure_fails_closed():
    router = Router(external=_PUBLIC)
    def broken(port, protocol):
        raise RuntimeError(501)
    router.getspecificportmapping = broken
    with pytest.raises(mod.IngressError, match='readback'):
        mod.manage('map', 4001, upnp=router)
    assert router.added == 0
