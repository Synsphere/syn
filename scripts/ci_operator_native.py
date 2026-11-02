"""Disposable real OS-keystore/operator-process acceptance, never a user identity.

Linux requires its own dbus-run-session and isolated XDG data directory. macOS
uses a newly created ephemeral native keychain and restores the prior default.
Only public receipts are retained. No mock keyring or HD provider is accepted.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time

from syn.operator.custody import _native_backend, backend_profile
from syn.operator.hd_keys import SLOTS
from syn.operator.service import initialize
from syn_client.operator import operator_status
from syn_client.operator_protocol import SynProtocolError, canonical
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
    control = directory / "keyring"
    control.mkdir(mode=0o700)
    daemon = subprocess.Popen(["/usr/bin/gnome-keyring-daemon", "--foreground", "--unlock", "--components=secrets",
                               "--control-directory", str(control)], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
    assert daemon.stdin is not None
    daemon.stdin.write(password.encode())
    daemon.stdin.close()
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                _native_backend().get_password("syn.fixture.probe", "absent")
                break
            except Exception:
                if time.monotonic() >= deadline or daemon.poll() is not None:
                    raise AssertionError("Real native Secret Service did not become ready") from None
                time.sleep(0.1)
        assert (data / "keyrings").is_dir(), "Native fixture did not use its isolated data directory"
        def lock():
            import secretstorage
            connection = secretstorage.dbus_init()
            secretstorage.Collection(connection).lock()
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
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" and not args.local_disposable_fixture:
        raise SystemExit("Explicit disposable-fixture scope is required outside CI")
    root = Path(__file__).resolve().parents[1]
    service, account = "syn.ci." + secrets.token_hex(8), "operator"
    with tempfile.TemporaryDirectory(prefix="syn-r03-", dir=Path("/tmp").resolve()) as name:
        base = Path(name).resolve()
        base.chmod(0o700)
        with native_fixture(base) as lock:
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
