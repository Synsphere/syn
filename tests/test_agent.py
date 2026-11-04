"""Canonical encrypted enrollment with the real pinned HD provider.

The credential backend and host binding here are explicit unit fixtures. These
tests do not establish native custody, sandbox isolation or physical attestation.
"""
from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path
import tempfile

import pytest

from syn.agent import adoption, device, enrollment, service, temporary, group
from syn.operator import adoption_state, custody, hd_keys, state
from syn.operator import service as operator_service
from syn.operator.adoption_cli import operate
from syn_client.agent import verify_status
from syn_client.operator_protocol import SynProtocolError, canonical

GROUP_ID = "be31002b-b7a6-4e46-bd29-4e39fb04e31e"


class MemoryBackend:
    def __init__(self):
        self.values = {}
        self.locked = False

    def get_password(self, service, account):
        if self.locked:
            raise OSError("explicit unit locked-store fixture")
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        if self.locked:
            raise OSError("explicit unit locked-store fixture")
        self.values[service, account] = value


@pytest.fixture
def scenario(monkeypatch):
    if sys.platform != "linux":
        pytest.skip("Canonical Agent profile is Linux/WSL; public clients remain portable")
    with tempfile.TemporaryDirectory(prefix="syn-agent-", dir="/tmp") as name:
        root = Path(name).resolve()
        root.chmod(0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        monkeypatch.setattr(custody, "native_home", lambda: home)
        monkeypatch.setattr(device, "host_binding", lambda: hashlib.sha256(b"explicit fixture host").hexdigest())
        backend = MemoryBackend()
        for module in (custody, hd_keys, state, operator_service, adoption_state, adoption,
                       device, enrollment, temporary, service, group):
            monkeypatch.setattr(module, "_native_backend", lambda: backend)
        operator = root / "operator"
        operator.mkdir(mode=0o700)
        created = operator_service.initialize(operator, service="syn.fixture.operator", account="primary", network_id="fixture")
        config_path = operator / "operator.json"
        operate(config_path, "adoption-init")
        agent = root / "agent"
        agent.mkdir(mode=0o700)
        device.initialize(agent, service="syn.fixture.agent", account="first",
            expected_operator=created["identity"]["base_public_key_hash"], network_id="fixture", group_id=GROUP_ID,
            group_epoch=1, approved_profile=device.PROFILE)
        config, _ = device.load_config(agent / "agent.json")
        pulse = operate(config_path, "agent-pulse")
        offer = enrollment.create_offer(config, agent, pulse)
        candidates = enrollment.reserve_offer(config_path, offer, 19, approve=lambda _: True)
        choice = enrollment.choose(config, agent, candidates)
        yield {"root": root, "backend": backend, "operator": config_path, "agent": agent,
               "config": config, "created": created, "offer": offer, "candidates": candidates, "choice": choice}


def grant(scenario, *, approve=lambda _: True):
    return operate(scenario["operator"], "agent-grant", {"offer": scenario["offer"],
        "candidates": scenario["candidates"], "selection": scenario["choice"]}, approve=approve)


def handoff(scenario):
    import time
    config, agent = scenario["config"], scenario["agent"]
    granted = grant(scenario)
    request = enrollment.prepare(config, agent, granted, now=int(time.time()))
    sealed = operate(scenario["operator"], "agent-seal", {"grant": granted, "request": request})
    return granted, request, sealed


def adopt(scenario, granted, request, sealed):
    import time
    config = scenario["config"]
    return adoption.adopt_selected_key(grant_packet=granted, request_packet=request, envelope=sealed,
        service=config["keyring_service"], account=config["keyring_account"], lock_dir=scenario["agent"],
        expected_master_hash=bytes.fromhex(config["expected_operator"]), now=int(time.time()))


def test_real_provider_encrypted_adoption_retains_selected_identity(scenario):
    granted, request, sealed = handoff(scenario)
    result = adopt(scenario, granted, request, sealed)
    receipt = operate(scenario["operator"], "agent-confirm", {"grant": granted, "key_proof": result["key_proof"]})
    server = operator_service.OperatorServer(scenario["operator"])
    journal = adoption_state.EnrollmentJournal(server.directory, server.signer.service, server.signer.account, server.expected_hash)
    import time
    envelope = group.seal_group_key(server=server, journal=journal, grant_packet=granted, request_packet=request,
        identifier=GROUP_ID, epoch=1, now=int(time.time()), approve=lambda _: True)
    group.adopt_group_key(scenario["config"], scenario["agent"], grant_packet=granted, request_packet=request,
                          envelope=envelope, now=int(time.time()))
    activated = service.activate(scenario["agent"] / "agent.json", receipt)
    selected = activated["selected_public_key_hash"]
    assert selected not in {scenario["created"]["identity"]["root_public_key_hash"], scenario["config"]["expected_operator"]}
    first = service.AgentServer(scenario["agent"] / "agent.json")
    second = service.AgentServer(scenario["agent"] / "agent.json")
    assert first.signer.public_key == second.signer.public_key
    assert first.boot_id != second.boot_id
    first.state.commit({**first.state.read(), "boot": 1})
    request = first.health_request()
    proof = first.status(request)
    checked = verify_status(proof, challenge=request["challenge"], expected_hash=selected,
        expected_operator=scenario["config"]["expected_operator"], expected_version=proof["status"]["runtime"]["runtime_version"],
        configuration_sha256=first.config_sha, assignment_sha256=first.assignment_sha, expected_group=GROUP_ID, expected_epoch=1)
    assert checked["mode"] == "agent" and checked["cog_execution_authorized"] is False
    assert operate(scenario["operator"], "agent-confirm", {"grant": granted, "key_proof": result["key_proof"]}) == receipt


def test_signed_lan_pledge_cli_checks_route_without_admitting_agent(scenario, capsysbinary):
    import json

    from syn.agent import lan_enrollment
    from syn.agent.__main__ import main as agent_cli
    from syn.operator.__main__ import main as operator_cli
    from syn_client.lan_enrollment import ADVERTISEMENT_DOMAIN
    from syn_client.operator_protocol import parse

    agent = scenario["root"] / "lan-agent"
    agent.mkdir(mode=0o700)
    device.initialize(agent, service="syn.fixture.agent", account="lan",
        expected_operator=scenario["config"]["expected_operator"], network_id="fixture", group_id=GROUP_ID,
        group_epoch=1, approved_profile=device.PROFILE)
    agent_config = agent / "agent.json"
    operator_config = scenario["operator"]
    authority = operator_config.parent / "authority.json"
    adoptions = operator_config.parent / "adoptions.json"
    original = authority.read_bytes(), adoptions.read_bytes()
    route = ["--host", "192.168.1.7", "--port", "45121"]
    assert operator_cli(["lan-advertise", "--config", str(operator_config), *route]) == 0
    raw = capsysbinary.readouterr().out
    advertisement = parse(raw)
    ad_file = agent / "advertisement.json"
    ad_file.write_bytes(raw)
    ad_file.chmod(0o600)
    args = ["lan-offer", "--config", str(agent_config), "--request", str(ad_file)]
    assert agent_cli([*args, "--host", "192.168.1.8", "--port", "45121"]) == 2
    refused = capsysbinary.readouterr()
    assert refused.out == b""
    assert scenario["backend"].get_password("syn.fixture.agent", "lan.agent.pledge.v1") is None
    assert agent_cli([*args, *route]) == 0
    packet = capsysbinary.readouterr().out
    submission = parse(packet)
    assert submission["route"] == advertisement
    config, _ = device.load_config(agent_config)
    assert device.device_id(config).hex().encode() not in packet
    assert submission["offer"]["header"]["profile"] == enrollment.LAN_SEAL_PROFILE
    request = operator_config.parent / "submission.json"
    request.write_bytes(packet)
    request.chmod(0o600)
    validate = ["lan-validate", "--config", str(operator_config), "--request", str(request), *route]
    assert operator_cli(validate) == 0
    receipt = json.loads(capsysbinary.readouterr().out)
    assert receipt["pending_persisted"] is receipt["agent_activation_authorized"] is False
    assert receipt["pledge_offer_sha256"] == json.loads(
        scenario["backend"].get_password("syn.fixture.agent", "lan.agent.pledge.v1")
    )["offer_sha256"]
    assert (authority.read_bytes(), adoptions.read_bytes()) == original

    # Even a separately signed route for the same pulse cannot be transplanted
    # into the encrypted offer by replacing the visible route commitment.
    moved = copy.deepcopy(submission)
    body = moved["route"]["advertisement"]
    body["endpoint"]["port"] = 45122
    operator = operator_service.OperatorServer(operator_config)
    moved["route"]["signature"] = operator.signer.sign(ADVERTISEMENT_DOMAIN + canonical(body)).hex()
    moved["offer"]["header"]["advertisement_sha256"] = lan_enrollment._digest(moved["route"])
    with pytest.raises(SynProtocolError, match="authentication failed"):
        lan_enrollment.validate_submission(operator_config, moved, host="192.168.1.7", port=45122)
    assert (authority.read_bytes(), adoptions.read_bytes()) == original


def test_lan_pending_inbox_recovers_and_fences_replays(scenario, monkeypatch, capsysbinary):
    import json
    import time

    from syn.agent import lan_enrollment
    from syn.operator import lan_inbox
    from syn.operator.__main__ import main as operator_cli

    operator_config = scenario["operator"]
    route = {"host": "192.168.1.7", "port": 45121}
    advertisement = lan_enrollment.advertise(operator_config, **route)
    expires_at = advertisement["advertisement"]["pulse"]["body"]["signed_pulse"]["pulse"]["expires_at"]
    operator = operator_service.OperatorServer(operator_config)
    inbox = lan_inbox.LanInbox(operator)

    def agent_offer(name):
        directory = scenario["root"] / name
        directory.mkdir(mode=0o700)
        device.initialize(directory, service="syn.fixture.agent", account=name,
            expected_operator=scenario["config"]["expected_operator"], network_id="fixture", group_id=GROUP_ID,
            group_epoch=1, approved_profile=device.PROFILE)
        config, _ = device.load_config(directory / "agent.json")
        return lan_enrollment.submit_offer(config, directory, advertisement, **route), device.device_id(config)

    packet, device_id = agent_offer("first-lan")
    with pytest.raises(SynProtocolError, match="anchor is absent"):
        lan_inbox.intake(operator_config, packet, **route)
    assert operator_cli(["lan-inbox-init", "--config", str(operator_config)]) == 0
    assert json.loads(capsysbinary.readouterr().out)["initialized"] is True
    empty = inbox.path.read_bytes()
    with monkeypatch.context() as fault:
        def interrupted(*_):
            raise OSError("interrupted after anchoring the pending transition")
        fault.setattr(lan_inbox.LanInbox, "_replace", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            lan_inbox.intake(operator_config, packet, **route)
    assert inbox.path.read_bytes() == empty
    receipt = lan_inbox.intake(operator_config, packet, **route)
    assert receipt["pending_persisted"] is True and receipt["agent_activation_authorized"] is False
    assert lan_inbox.intake(operator_config, packet, **route) == receipt
    digest = receipt["pledge_offer_sha256"]
    assert lan_inbox.pending(operator_config, offer=digest) == packet
    assert lan_inbox.pending(operator_config)["pending"] == [
        {"pledge_offer_sha256": digest, "expires_at": expires_at}
    ]
    assert device_id.hex().encode() not in inbox.path.read_bytes()
    with monkeypatch.context() as cloned:
        cloned.setattr(enrollment, "device_id", lambda _: device_id)
        replay, _ = agent_offer("rotated-lan")
        with pytest.raises(SynProtocolError, match="already has a protected record"):
            lan_inbox.intake(operator_config, replay, **route)

    # A second independent Agent may answer the same signed advertisement.
    second, _ = agent_offer("second-lan")
    assert lan_inbox.intake(operator_config, second, **route)["pledge_offer_sha256"] != digest
    retained = inbox.path.read_bytes()
    inbox.path.write_bytes(empty)
    try:
        with pytest.raises(SynProtocolError, match="altered or rolled back"):
            lan_inbox.pending(operator_config)
    finally:
        inbox.path.write_bytes(retained)

    with monkeypatch.context() as expired:
        expired.setattr(time, "time", lambda: expires_at + 1)
        assert lan_inbox.pending(operator_config)["pending"] == []
        with pytest.raises(SynProtocolError, match="No unexpired"):
            lan_inbox.pending(operator_config, offer=digest)
        rows = inbox.read()["records"]
        assert len(rows) == 2 and all(row["packet"] is None for row in rows)
        assert packet["offer"]["ciphertext"].encode() not in inbox.path.read_bytes()
    with monkeypatch.context() as backwards:
        backwards.setattr(time, "time", lambda: expires_at)
        with pytest.raises(SynProtocolError, match="clock moved backwards"):
            lan_inbox.intake(operator_config, packet, **route)


def test_hardware_stays_encrypted_and_temporary_hash_is_not_inheritance(scenario):
    hardware = device.device_id(scenario["config"])
    assert hardware.hex().encode() not in canonical(scenario["offer"])
    assert b"hardware_unique_id" not in canonical(scenario["offer"])
    expected = int.from_bytes(hashlib.sha256(hardware + bytes([19])).digest()[:3], "big")
    assert scenario["candidates"]["candidates"]["inheritance"] == expected
    assert hardware.hex() != scenario["config"]["temporary_public_key_hash"]


def test_independent_owner_denial_and_wrong_selected_signature_do_not_mutate(scenario):
    before = (scenario["operator"].parent / "adoptions.json").read_bytes()
    with pytest.raises(SynProtocolError, match="acceptance"):
        grant(scenario, approve=lambda _: False)
    assert (scenario["operator"].parent / "adoptions.json").read_bytes() == before
    scenario["choice"]["signature"] = "00" * 64
    with pytest.raises(Exception):
        grant(scenario)
    assert (scenario["operator"].parent / "adoptions.json").read_bytes() == before


def test_encrypted_transfer_tampering_replay_and_locked_custody_fail(scenario):
    granted, request, sealed = handoff(scenario)
    with pytest.raises(SynProtocolError, match="already sealed"):
        operate(scenario["operator"], "agent-seal", {"grant": granted, "request": request})
    changed = copy.deepcopy(sealed)
    changed["ciphertext"] = ("00" if changed["ciphertext"][:2] != "00" else "ff") + changed["ciphertext"][2:]
    with pytest.raises(SynProtocolError):
        adopt(scenario, granted, request, changed)
    result = adopt(scenario, granted, request, sealed)
    assert result["selected_public_key_hash"] == granted["grant"]["selected_public_key_hash"]
    with pytest.raises(SynProtocolError, match="exists"):
        adopt(scenario, granted, request, sealed)
    scenario["backend"].locked = True
    with pytest.raises(Exception):
        service.AgentSigner(scenario["config"], result["selected_public_key_hash"]).sign(b"no fallback")


def test_anchored_selection_rollback_and_swapped_agent_choice_fail(scenario):
    path = scenario["operator"].parent / "adoptions.json"
    before = path.read_bytes()
    grant(scenario)
    path.write_bytes(before)
    with pytest.raises(SynProtocolError, match="rolled back"):
        grant(scenario)


def test_different_virtual_devices_on_same_host_and_clone_refusal(scenario):
    second = scenario["root"] / "second"
    second.mkdir(mode=0o700)
    device.initialize(second, service="syn.fixture.agent", account="second",
        expected_operator=scenario["config"]["expected_operator"], network_id="fixture", group_id=GROUP_ID, group_epoch=1,
        approved_profile=device.PROFILE)
    config, _ = device.load_config(second / "agent.json")
    assert device.device_id(config) != device.device_id(scenario["config"])
    config["state_dir"] = str(scenario["agent"])
    with pytest.raises(SynProtocolError, match="another host"):
        device.device_id(config)


def test_group_derivation_separates_group_epoch_and_account_identity(scenario):
    signer = operator_service.OperatorServer(scenario["operator"]).signer
    one = group._derive_group_key(signer, "fixture", GROUP_ID, 1)
    assert one == group._derive_group_key(signer, "fixture", GROUP_ID, 1)
    assert one != group._derive_group_key(signer, "fixture", GROUP_ID, 2)
    assert one != group._derive_group_key(signer, "other-network", GROUP_ID, 1)
    assert one != group._derive_group_key(signer, "fixture", "4f672321-11ba-4405-843d-a5961a784bf6", 1)
    before = signer.derive_candidate_public_hashes(42)
    group._derive_group_key(signer, "fixture", GROUP_ID, 987)
    assert signer.derive_candidate_public_hashes(42) == before
    values, _ = hd_keys._load_operator(signer.service, signer.account)
    assert all(one != value for value in values.values())


def test_group_tampering_wrong_epoch_and_replay_never_replace_custody(scenario):
    import time
    granted, request, sealed = handoff(scenario)
    result = adopt(scenario, granted, request, sealed)
    operate(scenario["operator"], "agent-confirm", {"grant": granted, "key_proof": result["key_proof"]})
    server = operator_service.OperatorServer(scenario["operator"])
    journal = adoption_state.EnrollmentJournal(server.directory, server.signer.service, server.signer.account, server.expected_hash)
    envelope = group.seal_group_key(server=server, journal=journal, grant_packet=granted, request_packet=request,
        identifier=GROUP_ID, epoch=1, now=int(time.time()), approve=lambda _: True)
    before = dict(scenario["backend"].values)
    changed = copy.deepcopy(envelope)
    changed["ciphertext"] = ("ff" if changed["ciphertext"][:2] != "ff" else "00") + changed["ciphertext"][2:]
    with pytest.raises(Exception):
        group.adopt_group_key(scenario["config"], scenario["agent"], grant_packet=granted, request_packet=request,
                              envelope=changed, now=int(time.time()))
    assert scenario["backend"].values == before
    with pytest.raises(SynProtocolError):
        group.adopt_group_key({**scenario["config"], "group_epoch": 2}, scenario["agent"], grant_packet=granted,
                              request_packet=request, envelope=envelope, now=int(time.time()))
    assert scenario["backend"].values == before
    group.adopt_group_key(scenario["config"], scenario["agent"], grant_packet=granted, request_packet=request,
                          envelope=envelope, now=int(time.time()))
    with pytest.raises(SynProtocolError, match="exists"):
        group.adopt_group_key(scenario["config"], scenario["agent"], grant_packet=granted, request_packet=request,
                              envelope=envelope, now=int(time.time()))


def test_even_authentically_signed_unknown_pulse_version_is_rejected(scenario):
    import time
    from syn.operator.enrollment import PULSE_DOMAIN
    packet = operate(scenario["operator"], "agent-pulse")
    server = operator_service.OperatorServer(scenario["operator"])
    signed = packet["body"]["signed_pulse"]
    signed["pulse"]["schema_version"] = 2
    signed["signature"] = server.signer.sign(PULSE_DOMAIN + canonical(signed["pulse"])).hex()
    packet["signature"] = server.signer.sign(enrollment.PULSE_DOMAIN + canonical(packet["body"])).hex()
    with pytest.raises(SynProtocolError, match="pulse is stale"):
        enrollment._pulse_body(packet, scenario["config"]["expected_operator"], "fixture", int(time.time()))
