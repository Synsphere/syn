"""Native adapter contracts; no router or real network is mutated by these tests."""
import io
import subprocess
import threading
from types import SimpleNamespace

import pytest
import trio

import syn.upnp as upnp


class Gateway:
    lanaddr = "192.168.1.10"
    external = "93.184.216.34"
    discover_result = 1
    select_error = None
    add_result = True
    delete_result = True
    delete_removes = True
    force_missing = False
    count_error = False
    incomplete_table = False
    permanent_only = False
    control_url = None

    def __init__(self):
        self.table, self.added, self.deleted = {}, [], []
        self.selected = self.discovered = 0

    def discover(self):
        self.discovered += 1
        if isinstance(self.discover_result, Exception):
            raise self.discover_result
        return self.discover_result

    def selectigd(self):
        self.selected += 1
        if self.select_error:
            raise self.select_error
        return self.control_url

    def externalipaddress(self):
        return self.external

    def getspecificportmapping(self, port, protocol):
        return None if self.force_missing else self.table.get((port, protocol))

    def getportmappingnumberofentries(self):
        if self.count_error:
            raise OSError("Synthetic unverified query")
        return len(self.table)

    def getgenericportmapping(self, index):
        if self.incomplete_table:
            return None
        (port, protocol), (host, internal, description, enabled, lease) = list(self.table.items())[index]
        return port, protocol, (host, internal), description, "1" if enabled else "0", "", lease

    def addportmapping(self, port, protocol, host, internal, description, remote, lease):
        self.added.append((port, protocol, host, internal, description, remote, lease))
        if self.permanent_only and lease:
            raise Exception("OnlyPermanentLeasesSupported")
        if self.add_result:
            self.table[(port, protocol)] = (host, internal, description, True, lease)
        return self.add_result

    def deleteportmapping(self, port, protocol):
        self.deleted.append((port, protocol))
        if self.delete_result and self.delete_removes:
            self.table.pop((port, protocol), None)
        return self.delete_result


def manager(gateway, **kwargs):
    def factory(**options):
        gateway.multicastif = options["multicastif"]
        gateway.discoverdelay = options["discoverdelay"]
        return gateway
    return upnp.SynUpnpManager(gateway_factory=factory,
        environment_probe=lambda: {"platform": "linux", "wsl": False, "networkingMode": None}, **kwargs)


@pytest.mark.parametrize("result, devices", [(0, 0), (Exception("Success"), None), (-1, None), (True, None)])
def test_discovery_never_invents_a_device_count_or_selects_without_one(result, devices):
    gateway = Gateway()
    gateway.discover_result = result
    async def exercise():
        service = manager(gateway)
        assert not await service.discover()
        assert gateway.selected == 0
        assert service.status()["devices"] == devices
        assert service.status()["state"] == "unavailable"
        assert service.get_external_ip() is None
    trio.run(exercise)


@pytest.mark.parametrize("external", ["192.168.1.2", "100.64.1.2", "127.0.0.1", "0.0.0.0", "224.0.0.1", "::1", "bad"])
def test_unusable_gateway_address_is_never_exposed(external):
    gateway = Gateway()
    gateway.external = external
    async def exercise():
        service = manager(gateway)
        assert not await service.discover()
        assert service.get_external_ip() is None
        assert service.status()["externalIp"] is None
        assert not await service.add_port_mapping(4001)
    trio.run(exercise)


def test_discovery_failure_clears_a_previously_accepted_external_address():
    gateway = Gateway()
    async def exercise():
        service = manager(gateway, multicast_interface="192.168.1.10", discover_delay_ms=1500)
        assert await service.discover()
        assert gateway.multicastif == "192.168.1.10" and gateway.discoverdelay == 1500
        assert service.get_external_ip() == gateway.external
        gateway.select_error = RuntimeError("No UPnPdevice")
        assert not await service.discover()
        assert service.get_external_ip() is None
        assert service.status()["devices"] == 1
    trio.run(exercise)


def test_verified_mapping_renews_and_cleanup_deletes_only_its_own_marker():
    gateway = Gateway()
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        assert await service.add_port_mapping(4001)
        entry = service.status()["mappings"][0]
        assert entry["owned"] and entry["verified"] and entry["leaseSeconds"] == 3600
        assert entry["internalAddress"] == gateway.lanaddr
        assert service.status()["inboundReachabilityVerified"] is False
        assert await service.add_port_mapping(4001)
        assert len(gateway.added) == 2 and gateway.added[0][4] == gateway.added[1][4]
        # Status is detached from mutable internal evidence.
        entry["verified"] = False
        assert service.status()["mappings"][0]["verified"]
        assert await service.remove_port_mapping(4001)
        assert gateway.deleted == [(4001, "TCP")]
        assert not service.status()["mappings"]
    trio.run(exercise)


@pytest.mark.parametrize("host", ["192.168.1.10", "192.168.1.99"])
def test_preexisting_mapping_is_neither_replaced_nor_deleted(host):
    gateway = Gateway()
    gateway.table[(4001, "TCP")] = (host, 4001, "Another application", True, 0)
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        assert not await service.add_port_mapping(4001)
        assert not await service.remove_port_mapping(4001)
        assert not gateway.added and not gateway.deleted
    trio.run(exercise)


@pytest.mark.parametrize("failure", ["unverified-query", "truncated-enumeration"])
def test_ambiguous_absence_never_authorizes_overwrite(failure):
    gateway = Gateway()
    gateway.table[(4001, "TCP")] = (gateway.lanaddr, 4001, "Another application", True, 0)
    gateway.force_missing = True  # miniupnpc also returns None on query errors.
    gateway.count_error = failure == "unverified-query"
    gateway.incomplete_table = failure == "truncated-enumeration"
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        assert not await service.add_port_mapping(4001)
        assert not gateway.added
    trio.run(exercise)


def test_false_native_mapping_acknowledgement_is_not_success():
    gateway = Gateway()
    gateway.add_result = False
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        assert not await service.add_port_mapping(4001)
        assert not service.status()["mappings"][0]["verified"]
        assert await service.remove_port_mapping(4001)  # Confirmed already absent.
        assert not gateway.deleted
    trio.run(exercise)


@pytest.mark.parametrize("failure", ["false-acknowledgement", "still-present", "new-owner"])
def test_removal_requires_ownership_and_confirmed_absence(failure):
    gateway = Gateway()
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        assert await service.add_port_mapping(4001)
        gateway.delete_result = failure != "false-acknowledgement"
        gateway.delete_removes = failure != "still-present"
        if failure == "new-owner":
            gateway.table[(4001, "TCP")] = (gateway.lanaddr, 4001, "New owner", True, 0)
        assert not await service.remove_port_mapping(4001)
        assert (4001, "TCP") in gateway.table
        if failure == "new-owner":
            assert not gateway.deleted
        else:
            assert not service.status()["mappings"][0]["verified"]
    trio.run(exercise)


def test_permanent_lease_fallback_requires_explicit_router_response():
    gateway = Gateway()
    gateway.permanent_only = True
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        assert await service.add_port_mapping(4001)
        assert [call[-1] for call in gateway.added] == [3600, 0]
        assert service.status()["mappings"][0]["leaseSeconds"] == 0
    trio.run(exercise)


@pytest.mark.parametrize("mode", ["nat", "virtioproxy", "none"])
def test_wsl_nat_does_not_claim_router_mapping_exposes_guest(mode):
    gateway = Gateway()
    async def exercise():
        service = upnp.SynUpnpManager(gateway_factory=lambda **kwargs: gateway,
            environment_probe=lambda: {"platform": "linux", "wsl": True, "networkingMode": mode})
        assert not await service.discover()
        assert not gateway.discovered
        assert "WSL" in service.status()["reason"]
        assert service.status()["environment"]["networkingMode"] == mode
    trio.run(exercise)


def test_native_binding_interface_is_passed_to_constructor(monkeypatch):
    # The real extension rejects assignment to multicastif. Exercise its real
    # constructor without running SSDP or changing the user's network.
    native = upnp.miniupnpc.UPnP(multicastif="192.168.1.10", discoverdelay=1200)
    with pytest.raises((AttributeError, TypeError)):
        native.multicastif = "192.168.1.11"
    options_seen = []

    class ReadOnlyGateway(Gateway):
        @property
        def multicastif(self):
            return self.options["multicastif"]

    gateway = ReadOnlyGateway()
    def factory(**options):
        options_seen.append(options)
        gateway.options = options
        return gateway
    async def exercise():
        service = upnp.SynUpnpManager(multicast_interface="192.168.1.10",
            discover_delay_ms=1200, gateway_factory=factory,
            environment_probe=lambda: {"wsl": False})
        assert await service.discover()
    trio.run(exercise)
    assert options_seen == [{"multicastif": "192.168.1.10", "discoverdelay": 1200}]


def soap_fault(code):
    return (f'<s:Envelope xmlns:s="{upnp.SOAP_ENVELOPE}"><s:Body><s:Fault>'
            '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>'
            f'<UPnPError xmlns="{upnp.UPNP_CONTROL}"><errorCode>{code}</errorCode>'
            '<errorDescription>test response</errorDescription></UPnPError></detail>'
            '</s:Fault></s:Body></s:Envelope>').encode()


def soap_mapping(service, description="Another application"):
    return (f'<s:Envelope xmlns:s="{upnp.SOAP_ENVELOPE}"><s:Body>'
            f'<u:GetSpecificPortMappingEntryResponse xmlns:u="{service}">'
            '<NewInternalPort>4001</NewInternalPort><NewInternalClient>192.168.1.10</NewInternalClient>'
            f'<NewEnabled>1</NewEnabled><NewPortMappingDescription>{description}</NewPortMappingDescription>'
            '<NewLeaseDuration>3600</NewLeaseDuration></u:GetSpecificPortMappingEntryResponse>'
            '</s:Body></s:Envelope>').encode()


def mock_soap(monkeypatch, replies):
    calls = []
    pending = iter(replies)
    class Connection:
        def __init__(self, host, port, **options):
            self.call = {"host": host, "port": port, **options, "closed": False}
            calls.append(self.call)
        def request(self, method, path, data, headers):
            self.call.update(method=method, path=path, body=data, headers=headers)
        def getresponse(self):
            result = next(pending)
            if isinstance(result, Exception):
                raise result
            status, payload = result
            return SimpleNamespace(status=status, read=io.BytesIO(payload).read)
        def close(self):
            self.call["closed"] = True
    monkeypatch.setattr(upnp.http.client, "HTTPConnection", Connection)
    return calls


def test_standard_fault_714_allows_mapping_without_nonstandard_count(monkeypatch):
    gateway = Gateway()
    gateway.control_url = "http://192.168.1.1:49000/upnp?control=WANPPPConnection"
    gateway.count_error = True
    # First namespace unsupported, second namespace (PPP) confirms absence.
    calls = mock_soap(monkeypatch, [(500, soap_fault(401)), (500, soap_fault(714)),
                                  (500, soap_fault(714))])
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        assert await service.add_port_mapping(4001)
        assert await service.remove_port_mapping(4001)
        assert not service.status()["mappings"]
    trio.run(exercise)
    assert len(gateway.added) == len(gateway.deleted) == 1
    assert len(calls) == 3
    assert calls[0]["headers"]["SOAPAction"].startswith(f'"{upnp.WAN_SERVICES[0]}#')
    assert all(call["headers"]["SOAPAction"].startswith(f'"{upnp.WAN_SERVICES[1]}#') for call in calls[1:])
    assert all(call["host"] == "192.168.1.1" and call["port"] == 49000
        and call["path"] == "/upnp?control=WANPPPConnection"
        and call["method"] == "POST" and call["timeout"] == 3 and call["closed"] for call in calls)
    assert all(b"AddPortMapping" not in call["body"] and b"DeletePortMapping" not in call["body"]
               for call in calls)


@pytest.mark.parametrize("reply", [
    (500, soap_fault(606)), (500, soap_fault(718)), (200, soap_fault(714)),
    (302, soap_fault(714)), (500, b"<unrelated><errorCode>714</errorCode></unrelated>"),
    (500, b"x" * (upnp.MAX_SOAP_BYTES + 1)), TimeoutError("read timed out"),
    (500, b'<!DOCTYPE foo [<!ENTITY a "714">]>' + soap_fault(714)),
    (500, ('<!DOCTYPE foo [<!ENTITY a "714">]>' + soap_fault(714).decode()).encode("utf-16")),
])
def test_ambiguous_standard_lookup_does_not_write_or_fall_back_to_empty_count(monkeypatch, reply):
    gateway = Gateway()
    gateway.control_url = "http://192.168.1.1/upnp/control"
    calls = mock_soap(monkeypatch, [reply])
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        assert not await service.add_port_mapping(4001)
        assert not gateway.added and not gateway.deleted
    trio.run(exercise)
    assert len(calls) == 1 and calls[0]["closed"]


def test_standard_lookup_detects_foreign_mapping_hidden_by_binding(monkeypatch):
    gateway = Gateway()
    gateway.control_url = "http://192.168.1.1/upnp/control"
    gateway.force_missing = gateway.count_error = True
    calls = mock_soap(monkeypatch, [(200, soap_mapping(upnp.WAN_SERVICES[0]))])
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        assert not await service.add_port_mapping(4001)
        assert "another mapping" in service.status()["reason"]
        assert not gateway.added
    trio.run(exercise)
    assert len(calls) == 1


@pytest.mark.parametrize("cleanup_succeeds", [True, False])
def test_gateway_change_retains_old_cleanup_handle_until_resolved(monkeypatch, cleanup_succeeds):
    first, second = Gateway(), Gateway()
    first.control_url, second.control_url = "http://192.168.1.1/wan", "http://192.168.1.2/wan"
    first.delete_removes = cleanup_succeeds
    gateways = iter([first, second])
    responses = [(500, soap_fault(714))]
    if cleanup_succeeds:
        responses += [(500, soap_fault(714)), (500, soap_fault(714))]
    mock_soap(monkeypatch, responses)
    async def exercise():
        service = upnp.SynUpnpManager(gateway_factory=lambda **kwargs: next(gateways),
                                     environment_probe=lambda: {"wsl": False})
        assert await service.discover()
        assert await service.add_port_mapping(4001)
        assert await service.discover()
        assert bool(await service.add_port_mapping(4001)) is cleanup_succeeds
        if cleanup_succeeds:
            assert not first.table and len(second.added) == 1
            assert service._owned[(4001, "TCP")]["gateway"] is second
        else:
            assert first.table and not second.added
            assert service._owned[(4001, "TCP")]["gateway"] is first
            assert service.status()["state"] == "cleanup-failed"
        assert first.deleted == [(4001, "TCP")]
    trio.run(exercise)


def test_wsl_network_probe_is_bounded_and_does_not_change_configuration(monkeypatch):
    monkeypatch.setattr(upnp.sys, "platform", "linux")
    monkeypatch.setattr(upnp.platform, "release", lambda: "6.6.0-microsoft-standard-WSL2")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="mirrored\n")
    monkeypatch.setattr(upnp.subprocess, "run", run)
    assert upnp.network_environment()["networkingMode"] == "mirrored"
    assert calls[0][0] == ["wslinfo", "--networking-mode"]
    assert calls[0][1]["timeout"] == 2
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 2)
    monkeypatch.setattr(upnp.subprocess, "run", unavailable)
    assert upnp.network_environment()["networkingMode"] == "unknown"


def test_cancellation_waits_for_native_mapping_and_retains_cleanup_evidence():
    gateway = Gateway()
    entered, finish = threading.Event(), threading.Event()
    add = gateway.addportmapping
    def blocked(*args):
        entered.set()
        assert finish.wait(5)
        return add(*args)
    gateway.addportmapping = blocked
    async def exercise():
        service = manager(gateway)
        assert await service.discover()
        done = trio.Event()
        scope = trio.CancelScope()
        async def mapping():
            with scope:
                await service.add_port_mapping(4001)
            done.set()
        async with trio.open_nursery() as nursery:
            nursery.start_soon(mapping)
            assert await trio.to_thread.run_sync(lambda: entered.wait(3))
            scope.cancel()
            await trio.lowlevel.checkpoint()
            assert not done.is_set()
            finish.set()
            with trio.fail_after(3):
                await done.wait()
            assert service.status()["mappings"][0]["verified"]
            assert await service.remove_port_mapping(4001)
            nursery.cancel_scope.cancel()
    trio.run(exercise)
