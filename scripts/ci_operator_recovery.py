"""Exercise real operator recovery in two disposable native credential stores."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import subprocess
import sys
import tempfile
import termios
import time
from unittest.mock import patch

from mnemonic import Mnemonic

from ci_operator_native import child_environment, native_fixture, ready, start, stop
from syn.agent import device, enrollment, lan_enrollment
from syn.operator import lan_inbox
from syn.operator.adoption_cli import operate
from syn.operator.custody import _native_backend, backend_profile
from syn.operator.recovery import RECEIVER_SUFFIX, _states
from syn.operator.service import OperatorServer, initialize
from syn.operator.state import write_new
from syn_client.operator_protocol import SynProtocolError

GROUP = "be31002b-b7a6-4e46-bd29-4e39fb04e31e"
ROUTE = {"host": "192.168.77.1", "port": 45121}
BACKUP_PROMPT = re.compile(rb"Type (BACKUP [0-9a-f]{12}) to create")
RESTORE_PROMPT = re.compile(rb"Type (RESTORE [0-9a-f]{12} [0-9a-f]{12}) to restore")
PASSWORD_PROMPT = b"Operator recovery phrase (private, no echo): "
WORDS = re.compile(rb"Private recovery phrase:\r?\n([a-z]+(?: [a-z]+){23})\r?\n")


def terminal_process(*args):
    master, slave = pty.openpty()

    def foreground():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpgrp())

    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-m", "syn.operator", *map(str, args)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(slave,),
            preexec_fn=foreground,
            env=child_environment(),
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return process, master


def conversation(owned, prompt, *, phrase=None):
    process, master = owned
    captured = bytearray()
    approved = entered = False
    deadline = time.monotonic() + 35
    try:
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.1)[0]:
                if process.poll() is not None:
                    break
                continue
            try:
                part = os.read(master, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not part:
                break
            captured.extend(part)
            if len(captured) > 8192:
                raise AssertionError("Recovery terminal exceeded its fixture bound")
            approval = prompt.search(captured)
            if approval and not approved:
                os.write(master, approval[1] + b"\n")
                approved = True
            if phrase and PASSWORD_PROMPT in captured and not entered:
                os.write(master, phrase + b"\n")
                entered = True
        stdout, stderr = process.communicate(timeout=5)
        words = WORDS.search(captured)
        secret = phrase if phrase is not None else (words[1] if words else None)
        if secret is not None:
            if secret in stdout or secret in stderr or (phrase is not None and secret in captured):
                raise AssertionError("Recovery phrase escaped the allowed terminal surface")
        if process.returncode != 0 and stdout:
            raise AssertionError("Refused recovery produced a success receipt")
        return (
            process.returncode,
            json.loads(stdout) if stdout else None,
            words[1] if words else None,
            entered,
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        os.close(master)


def make_agent(base, account, pin):
    directory = base / account
    directory.mkdir(mode=0o700)
    device.initialize(
        directory,
        service="syn.fixture.recovery.agent",
        account=account,
        expected_operator=pin,
        network_id="ci-recovery",
        group_id=GROUP,
        group_epoch=1,
        approved_profile=device.PROFILE,
    )
    return directory, device.load_config(directory / "agent.json")[0]


def protected_enrollment(base, config, pin):
    operate(config, "adoption-init")
    lan_inbox.initialize(config)
    # macOS qualifies the operator keychain. The Agent side is only a fixture client.
    with patch.object(device, "host_binding", return_value="8" * 64):
        agent, cfg = make_agent(base, "first", pin)
        packet = enrollment.create_offer(cfg, agent, operate(config, "agent-pulse"))
        candidates = enrollment.reserve_offer(config, packet, 29, approve=lambda _: True)
        selected = enrollment.choose(cfg, agent, candidates)
        grant = operate(
            config,
            "agent-grant",
            {
                "offer": packet,
                "candidates": candidates,
                "selection": selected,
            },
            approve=lambda _: True,
        )
        prepare = enrollment.prepare(cfg, agent, grant, now=int(time.time()))
        operate(config, "agent-seal", {"grant": grant, "request": prepare})
        pending_dir, pending_config = make_agent(base, "second", pin)
        advertisement = lan_enrollment.advertise(config, **ROUTE)
        pledge = lan_enrollment.submit_offer(pending_config, pending_dir, advertisement, **ROUTE)
        pending = lan_inbox.intake(config, pledge, **ROUTE)
    return pending["pledge_offer_sha256"], pledge, grant, prepare


def checkpoint(node, config, created, destination):
    process = start(node)
    try:
        proof = ready(process, node, created)
    finally:
        stop(process)
    code, receipt, phrase, _ = conversation(
        terminal_process("backup-recovery", "--config", config, "--output", destination),
        BACKUP_PROMPT,
    )
    assert (
        code == 0
        and phrase is not None
        and receipt["base_public_key_hash"] == created["identity"]["base_public_key_hash"]
    )
    return proof, receipt, phrase


def destination_arguments(node, backup, pin, digest, account):
    return (
        "recover-operator",
        "--state-dir",
        node,
        "--keyring-service",
        "syn.fixture.recovery.destination",
        "--keyring-account",
        account,
        "--backup",
        backup,
        "--expected-base-public-key-hash",
        pin,
        "--expected-backup-sha256",
        digest,
    )


def _fixture(base):
    source_store, destination_store = base / "source-store", base / "destination-store"
    node = base / "original"
    for path in (source_store, destination_store, node):
        path.mkdir(mode=0o700)
    config = node / "operator.json"
    account = "operator"
    service = "syn.fixture.recovery." + secrets.token_hex(8)
    old_backup, backup = base / "older.bin", base / "latest.bin"
    with native_fixture(source_store):
        created = initialize(node, service=service, account=account, network_id="ci-recovery")
        pin = created["identity"]["base_public_key_hash"]
        _, old_receipt, _ = checkpoint(node, config, created, old_backup)
        stale_control = (node / "authority.json").read_bytes()
        offer, pledge, grant, request = protected_enrollment(base, config, pin)
        original_proof, receipt, phrase = checkpoint(node, config, created, backup)
        assert old_receipt["backup_sha256"] != receipt["backup_sha256"]
        assert len(receipt["state_sha256"]) == 3
        before = {
            name: state.read() for name, state in _states(node, service, account, pin).items()
        }
        receiver = _native_backend().get_password(service, account + RECEIVER_SUFFIX)
        receiver_sha256 = hashlib.sha256(receiver.encode("ascii")).hexdigest()
        assert len(before["control"]["reservations"]) == 1
        assert [row["kind"] for row in before["adoption"]["events"]] == [
            "selected",
            "granted",
            "sealed",
        ]
        assert before["lan"]["records"][0]["offer"] == offer

    # Security.framework can cache the selected keychain inside a process.
    # A clean verifier process selects the fresh destination, like a real restore.
    context = {
        "service": service,
        "account": account,
        "created": created,
        "pin": pin,
        "receipt": receipt,
        "original_proof": original_proof,
        "before": before,
        "phrase": phrase.decode("ascii"),
        "receiver_sha256": receiver_sha256,
        "stale_control": stale_control.decode("ascii"),
        "offer": offer,
        "pledge": pledge,
        "grant": grant,
        "request": request,
    }
    with native_fixture(destination_store):
        result = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--destination", str(base)],
            input=json.dumps(context).encode("ascii"),
            stdout=subprocess.PIPE,
            env={**child_environment(), "GITHUB_ACTIONS": "true"},
            timeout=105,
            check=False,
        )
        if result.returncode or phrase in result.stdout:
            raise AssertionError("Fresh native destination verification failed")
        return json.loads(result.stdout)


def _destination(base, context):
    service, account = context["service"], context["account"]
    created, pin, receipt = context["created"], context["pin"], context["receipt"]
    original_proof, before = context["original_proof"], context["before"]
    phrase = context["phrase"].encode("ascii")
    stale_control = context["stale_control"].encode("ascii")
    offer, pledge, grant, request = (
        context[key] for key in ("offer", "pledge", "grant", "request")
    )
    old_backup, backup = base / "older.bin", base / "latest.bin"
    assert _native_backend().get_password(service, account + ".operator.entropy.v1") is None
    fresh = base / "fresh"
    competing = base / "competing"
    partial = base / "partial"
    for path in (fresh, competing, partial):
        path.mkdir(mode=0o700)
    digest = receipt["backup_sha256"]

    def args(path=fresh, file=backup, selected_pin=pin, selected_account="fresh"):
        return destination_arguments(path, file, selected_pin, digest, selected_account)

    stale = conversation(terminal_process(*args(file=old_backup)), RESTORE_PROMPT, phrase=phrase)
    assert stale == (2, None, None, False) and not any(fresh.iterdir())
    tampered = base / "tampered.bin"
    raw = backup.read_bytes()
    write_new(tampered, raw[:-1] + bytes([raw[-1] ^ 1]))
    corrupted = conversation(terminal_process(*args(file=tampered)), RESTORE_PROMPT, phrase=phrase)
    assert corrupted == (2, None, None, False) and not any(fresh.iterdir())
    wrong = Mnemonic("english").to_mnemonic(bytes(32)).encode("ascii")
    assert conversation(terminal_process(*args()), RESTORE_PROMPT, phrase=wrong) == (
        2,
        None,
        None,
        True,
    )
    assert conversation(
        terminal_process(*args(selected_pin="0" * 64)),
        RESTORE_PROMPT,
        phrase=phrase,
    ) == (2, None, None, True)
    assert not any(fresh.iterdir())

    _native_backend().set_password(
        "syn.fixture.recovery.destination", "partial.operator.control-state.v1", "partial"
    )
    assert conversation(
        terminal_process(*args(partial, selected_account="partial")),
        RESTORE_PROMPT,
        phrase=phrase,
    ) == (2, None, None, True)
    assert not any(partial.iterdir())
    assert (
        _native_backend().get_password(
            "syn.fixture.recovery.destination",
            "partial.operator.control-state.v1",
        )
        == "partial"
    )

    jobs = [
        terminal_process(*args()),
        terminal_process(*args(competing, selected_account="competing")),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda job: conversation(job, RESTORE_PROMPT, phrase=phrase), jobs)
        )
    assert sorted(item[0] for item in results) == [0, 2]
    winner = next(i for i, result in enumerate(results) if result[0] == 0)
    restored = results[winner][1]
    target, target_account = ((fresh, "fresh"), (competing, "competing"))[winner]
    loser = (competing, fresh)[winner]
    assert not any(loser.iterdir())
    assert (
        restored["identity"] == created["identity"]
        and restored["state_sha256"] == receipt["state_sha256"]
    )
    assert restored["agent_activation_authorized"] is False
    assert conversation(
        terminal_process(*args(target, selected_account=target_account)),
        RESTORE_PROMPT,
        phrase=phrase,
    ) == (2, None, None, True)

    restored_config = target / "operator.json"
    server = OperatorServer(restored_config)
    assert server.expected_hash == pin and server.config["network_id"] == "ci-recovery"
    states = _states(target, server.signer.service, server.signer.account, pin)
    assert {name: state.read() for name, state in states.items()} == before
    receiver = _native_backend().get_password(
        server.signer.service, server.signer.account + RECEIVER_SUFFIX
    )
    assert hashlib.sha256(receiver.encode("ascii")).hexdigest() == context["receiver_sha256"]
    assert lan_inbox.pending(restored_config, offer=offer) == pledge
    assert (
        lan_enrollment.validate_submission(restored_config, pledge, **ROUTE)["pledge_offer_sha256"]
        == offer
    )
    try:
        operate(restored_config, "agent-seal", {"grant": grant, "request": request})
    except SynProtocolError:
        pass
    else:
        raise AssertionError("Restored one-use adoption transition accepted replay")

    # Copying stale journal bytes must still be refused against the new native anchor.
    control = states["control"]
    retained = control.path.read_bytes()
    control.path.write_bytes(stale_control)
    try:
        control.read()
    except SynProtocolError:
        pass
    else:
        raise AssertionError("Restored allocation anchor accepted stale state")
    finally:
        control.path.write_bytes(retained)

    process = start(target)
    try:
        restored_proof = ready(process, target, restored)
    finally:
        stop(process)
    assert (
        restored_proof["status"]["base_public_key"] == original_proof["status"]["base_public_key"]
    )
    assert restored_proof["status"]["boot"] == original_proof["status"]["boot"] + 1
    return {
        "profile": "syn.operator-native-recovery-acceptance/1",
        "result": "passed",
        "native_custody": backend_profile(),
        "fresh_native_store": True,
        "pinned_operator_sha256": pin,
        "backup_sha256": digest,
        "preserved_state_sha256": receipt["state_sha256"],
        "original_readiness": original_proof,
        "restored_readiness": restored_proof,
        "restored_enrollment_receiver": True,
        "pending_pledge_reverified": True,
        "adoption_replay_refused": True,
        "wrong_phrase_wrong_pin_stale_tampered_and_partial_refused": True,
        "concurrent_recovery_one_winner": True,
        "overwrite_refused": True,
    }


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This acceptance harness requires an owned disposable CI host")
    if len(sys.argv) == 3 and sys.argv[1] == "--destination":
        context = json.loads(sys.stdin.buffer.read(250_000))
        print(json.dumps(_destination(Path(sys.argv[2]), context)))
        return
    official = len(sys.argv) == 3 and sys.argv[1] == "--official-runtime"
    if len(sys.argv) != 1 and not official:
        raise SystemExit("Unexpected recovery acceptance arguments")
    if official:
        from syn.operator import recovery

        runtime = Path(sys.argv[2]).resolve(strict=True)
        if Path(sys.prefix).resolve() != runtime or not Path(recovery.__file__).resolve().is_relative_to(runtime):
            raise AssertionError("Recovery must execute from the admitted official runtime")
    with tempfile.TemporaryDirectory(prefix="syn-recovery-", dir=Path("/tmp").resolve()) as work:
        receipt = _fixture(Path(work).resolve())
    receipt["official_runtime_executed"] = official
    output = Path("artifacts/operator/native-recovery.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    print(
        "Native recovery passed with both pinned identity and protected enrollment state.",
        flush=True,
    )


if __name__ == "__main__":
    main()
