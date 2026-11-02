"""Disposable real OS-keystore/operator-process acceptance, never a user identity.

Linux requires its own dbus-run-session and isolated XDG data directory. macOS
uses a newly created ephemeral native keychain and restores the prior default.
Only public receipts are retained. No mock keyring or HD provider is accepted.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import select
import signal
import subprocess
import sys
import tempfile
import time

from syn.operator.custody import _native_backend, backend_profile
from syn.operator.service import initialize
from syn_client.operator import operator_status
from syn_client.operator_protocol import SynProtocolError
from syn.release import runtimeContract


def checked(argv, *, input=None, timeout=20):
    try:
        result = subprocess.run(argv, input=input, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        # A native keychain CLI can require a disposable fixture password in
        # argv; never include that argv in a timeout/error diagnostic.
        raise AssertionError("Native fixture command unavailable or timed out") from None
    if result.returncode:
        raise AssertionError("Native fixture setup command failed")
    return result.stdout


def private_linux_unlock_fixture(password, environment, *, expect_refusal=False):
    """Exercise the actual installed human CLI with an ephemeral fixture only."""
    import errno
    import fcntl
    import pty
    import termios
    master, slave = pty.openpty()
    def foreground_terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(slave, os.getpgrp())
    process = subprocess.Popen([sys.executable, "-I", "-B", "-m", "syn.operator", "unlock"],
                               stdin=slave, stdout=slave, stderr=slave, env=environment,
                               preexec_fn=foreground_terminal)
    os.close(slave)
    output = bytearray()
    answered = set()
    prompts = (b"Native keystore password (private, no echo): ",
               b"Confirm new native keystore password: ")
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    part = os.read(master, 4096)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not part:
                    break
                output.extend(part)
                if len(output) > 16384:
                    raise AssertionError("Native unlock fixture output exceeded its bound")
                for index, prompt in enumerate(prompts):
                    if index not in answered and prompt in output:
                        os.write(master, password.encode() + b"\n")
                        answered.add(index)
            elif process.poll() is not None:
                break
        code = process.wait(timeout=2)
        ready = b'"ready":true' in output
        expected_result = (code == 2 and not ready) if expect_refusal else (code == 0 and ready)
        if not expected_result or password.encode() in output:
            # Never include the password, argv or terminal capture in diagnostics.
            raise AssertionError("Actual private native unlock fixture failed: "
                                 f"exit={code}, prompts={sorted(answered)}, "
                                 f"echoed={password.encode() in output}, "
                                 f"ready={ready}")
        return {"password_prompt": 0 in answered, "new_password_confirmed": 1 in answered,
                "password_echoed": False, "refused": code != 0}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)


def bounded_secret_service_connections():
    """Real native reads/writes must fit the Agent's unchanged descriptor limit."""
    code = """
import gc, json, resource
from pathlib import Path
from syn.operator.custody import _NoninteractiveSecretService
gc.disable()
resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
before = len(list(Path('/proc/self/fd').iterdir()))
backend = _NoninteractiveSecretService()
for index in range(200):
    assert backend.get_password('syn.fixture.connections', 'absent') is None
    backend.set_password('syn.fixture.connections', 'present', 'disposable-fixture')
    assert backend.get_password('syn.fixture.connections', 'present') == 'disposable-fixture'
after = len(list(Path('/proc/self/fd').iterdir()))
assert after <= before + 2, 'Native credential operations leaked owned descriptors'
print(json.dumps({'operations': 600, 'before_fds': before, 'after_fds': after, 'limit': 128}))
"""
    receipt = json.loads(checked([sys.executable, "-I", "-B", "-c", code], timeout=60))
    print("Native credential descriptor lifetime passed: " + json.dumps(receipt, sort_keys=True), flush=True)


@contextmanager
def native_fixture(directory: Path):
    """Actual encrypted native store with a random, ephemeral test password."""
    password = secrets.token_urlsafe(32)
    if sys.platform == "darwin":
        keychain = directory / "fixture.keychain-db"
        original = checked(["/usr/bin/security", "default-keychain", "-d", "user"]).decode().strip().strip('"')
        checked(["/usr/bin/security", "create-keychain", "-p", password, str(keychain)])
        try:
            checked(["/usr/bin/security", "set-keychain-settings", "-lut", "3600", str(keychain)])
            checked(["/usr/bin/security", "unlock-keychain", "-p", password, str(keychain)])
            checked(["/usr/bin/security", "default-keychain", "-d", "user", "-s", str(keychain)])
            yield lambda: checked(["/usr/bin/security", "lock-keychain", str(keychain)])
        finally:
            checked(["/usr/bin/security", "unlock-keychain", "-p", password, str(keychain)])
            checked(["/usr/bin/security", "default-keychain", "-d", "user", "-s", original])
            checked(["/usr/bin/security", "delete-keychain", str(keychain)])
        return
    if sys.platform != "linux" or "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        raise AssertionError("Native Linux acceptance requires an isolated D-Bus session")
    data = directory / "xdg-data"
    data.mkdir(mode=0o700)
    environment = dict(os.environ)
    environment["XDG_DATA_HOME"] = str(data)
    environment["XDG_RUNTIME_DIR"] = str(directory)
    control = directory / "keyring"
    control.mkdir(mode=0o700)
    daemon = subprocess.Popen(["/usr/bin/gnome-keyring-daemon", "--foreground", "--components=secrets",
                               "--control-directory", str(control)], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                # Do not let a readiness probe D-Bus-activate a competing
                # daemon before our foreground fixture acquires its name.
                reply = checked(["/usr/bin/dbus-send", "--session", "--dest=org.freedesktop.DBus",
                                 "--type=method_call", "--print-reply", "/org/freedesktop/DBus",
                                 "org.freedesktop.DBus.NameHasOwner", "string:org.freedesktop.secrets"])
                if b"boolean true" not in reply:
                    raise ValueError("owned native service is starting")
                break
            except Exception:
                if time.monotonic() >= deadline or daemon.poll() is not None:
                    raise AssertionError("Real native Secret Service did not become ready") from None
                time.sleep(0.1)
        first_unlock = private_linux_unlock_fixture(password, environment)
        assert first_unlock["new_password_confirmed"], "Fresh native store must confirm its password"
        _native_backend().get_password("syn.fixture.probe", "absent")
        assert (data / "keyrings").is_dir(), "Native fixture did not use its isolated data directory"
        def lock():
            import secretstorage
            with closing(secretstorage.dbus_init()) as connection:
                secretstorage.Collection(connection).lock()
        lock()
        refused = private_linux_unlock_fixture(secrets.token_urlsafe(32), environment, expect_refusal=True)
        assert refused["password_prompt"] and refused["refused"]
        import secretstorage
        with closing(secretstorage.dbus_init()) as connection:
            assert secretstorage.Collection(connection).is_locked()
        repeat_unlock = private_linux_unlock_fixture(password, environment)
        assert repeat_unlock["password_prompt"] and not repeat_unlock["new_password_confirmed"]
        _native_backend().get_password("syn.fixture.probe", "absent")
        print("Native private-terminal creation, no-echo, wrong-password refusal and repeated unlock passed", flush=True)
        bounded_secret_service_connections()
        yield lock
    finally:
        if daemon.poll() is None:
            os.killpg(daemon.pid, signal.SIGTERM)
        daemon.wait(timeout=8)


def child_environment():
    import pwd
    result = {"PATH": "/usr/bin:/bin", "HOME": pwd.getpwuid(os.getuid()).pw_dir,
              "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        if name in os.environ:
            result[name] = os.environ[name]
    return result


def start(directory: Path):
    return subprocess.Popen([sys.executable, "-I", "-B", "-m", "syn.operator", "serve", "--config",
                             str(directory / "operator.json")], env=child_environment(),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def stop(process, *, crash=False):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL if crash else signal.SIGTERM)
    process.wait(timeout=10)


def status(directory: Path, created):
    return operator_status(socket_path=directory / "operator.sock", expected_hash=created["identity"]["base_public_key_hash"],
                           expected_version=runtimeContract().runtime_version,
                           configuration_sha256=created["configuration_sha256"], owner_uid=os.getuid())


def ready(process, directory, created):
    deadline = time.monotonic() + 20
    while process.poll() is None:
        try:
            value = status(directory, created)
            assert value["status"]["pid"] == process.pid
            return value
        except (OSError, SynProtocolError, ValueError):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    raise AssertionError("Real operator process failed authenticated readiness")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-disposable-fixture", action="store_true")
    parser.add_argument("--consumer-python", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" and not args.local_disposable_fixture:
        raise SystemExit("Explicit disposable-fixture scope is required outside CI")
    root = Path(__file__).resolve().parents[1]
    service, account = "syn.ci." + secrets.token_hex(8), "operator"
    with tempfile.TemporaryDirectory(prefix="syn-r03-", dir=Path("/tmp").resolve()) as name:
        base = Path(name).resolve()
        base.chmod(0o700)
        with native_fixture(base) as lock:
            if args.consumer_python:
                if args.store is None or args.policy is None:
                    raise AssertionError("Explicit official installation/policy required")
                helper = root / "scripts/ci_operator_consumer.py"
                command = [str(args.consumer_python), "-I", "-B", str(helper),
                           "--store", str(args.store), "--policy", str(args.policy),
                           "--root", str(base / "deployment")]
                accepted = json.loads(checked(command, timeout=180))
                lock()
                locked = json.loads(checked(command + ["--expect-locked"], timeout=60))
                output = root / "artifacts/operator/official-deployment.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                receipt = {"profile": "syn.official-operator-deployment-acceptance/1",
                           "result": "passed", "custody": backend_profile(),
                           "qualification": "official_package_and_reviewed_potbot_supervisor_with_disposable_native_store",
                           "deployment": accepted, "locked_store": locked,
                           "agent_activation_authorized": False}
                output.write_text(json.dumps(receipt, sort_keys=True) + "\n")
                print(json.dumps({"result": "passed", "qualification": receipt["qualification"]}))
                return
            node = base / "node"
            node.mkdir(mode=0o700)
            created = initialize(node, service=service, account=account, network_id="ci-native")
            print("Native OS keystore initialized in a disposable fixture", flush=True)
            public_hash = created["identity"]["base_public_key_hash"]
            attestations = []
            process = None
            try:
                for crash in (False, True):
                    process = start(node)
                    attestations.append(ready(process, node, created))
                    stop(process, crash=crash)
                process = start(node)
                attestations.append(ready(process, node, created))
                assert len({a["status"]["boot_id"] for a in attestations}) == 3
                assert [a["status"]["boot"] for a in attestations] == [1, 2, 3]
                assert all(hashlib.sha256(bytes.fromhex(a["status"]["base_public_key"])).hexdigest() == public_hash for a in attestations)
                before = status(node, created)
                try:
                    initialize(node, service=service, account=account, network_id="ci-native")
                except SynProtocolError:
                    pass
                else:
                    raise AssertionError("Existing operator identity was replaced")
                after = status(node, created)
                assert after["status"]["base_public_key"] == before["status"]["base_public_key"]
                assert after["status"]["boot_id"] == before["status"]["boot_id"]
                lock()
                refused = False
                try:
                    status(node, created)
                except (OSError, SynProtocolError, ValueError):
                    refused = True
                assert refused, "A locked native key store still passed readiness"
                stop(process, crash=True)
                process = None
                output = root / "artifacts/operator/native.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                receipt = {"profile": "syn.operator-native-acceptance/1", "result": "passed", "custody": backend_profile(),
                           "runtime": runtimeContract().to_dict(), "same_identity_after_graceful_restart": True,
                           "same_identity_after_crash": True, "locked_native_store_refused": True,
                           "implicit_reinitialization_refused": True, "agent_activation_authorized": False,
                           "qualification": "disposable_native_software_keystore_and_real_operator_process",
                           "attestations": attestations}
                output.write_text(json.dumps(receipt, sort_keys=True) + "\n")
                print(json.dumps({k: v for k, v in receipt.items() if k not in {"attestations", "runtime"}}, sort_keys=True))
            finally:
                if process is not None:
                    stop(process, crash=True)
                # The enclosing fixture destroys only its own ephemeral native
                # collection/keychain; no production account is enumerated.


if __name__ == "__main__":
    main()
