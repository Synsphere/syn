"""Private native credential-service lifetime and bounded installer enrollment.

The inherited controller pipe carries public/encrypted enrollment frames only.
The native password is read exclusively by Syn's private controlling-terminal
prompt. This is not a socket, a Cog endpoint or an Agent messaging transport.
"""
from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import time

from syn.operator.custody import _agent_key_lock, private_directory
from syn.operator.unlock import unlock
from syn_client.agent import agent_status
from syn_client.operator_protocol import SynProtocolError, canonical, exact, parse
from . import adoption, device, enrollment, service


def _output(value):
    raw = canonical(value)
    if len(raw) > 65536:
        raise SynProtocolError("Agent public response exceeds its bound")
    sys.stdout.buffer.write(raw + b"\n")
    sys.stdout.buffer.flush()


def scope():
    status = Path("/proc/self/status").read_text()
    denied = []
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            with socket.socket(family, socket.SOCK_STREAM):
                denied.append(False)
        except OSError as error:
            denied.append(error.errno in {errno.EPERM, errno.EACCES})
    root = [row.split() for row in Path("/proc/self/mountinfo").read_text().splitlines() if row.split()[4] == "/"]
    return {"profile": "syn.agent-scope/1", "uid": os.getuid(), "gid": os.getgid(),
            "uid_map": [list(map(int, row.split())) for row in Path("/proc/self/uid_map").read_text().splitlines()],
            "capabilities_zero": all(f"{name}:\t0000000000000000\n" in status for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")),
            "no_new_privileges": "NoNewPrivs:\t1\n" in status, "internet_socket_creation_denied": all(denied),
            "root_read_only": len(root) == 1 and "ro" in root[0][5].split(","),
            "namespaces": {name: os.readlink(f"/proc/self/ns/{name}") for name in ("pid", "mnt", "net", "user", "ipc", "uts", "cgroup")}}


def _start_keyring():
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    private_directory(runtime)
    control = runtime / "keyring"
    control.mkdir(mode=0o700)
    os.environ["GNOME_KEYRING_CONTROL"] = str(control)
    daemon = subprocess.Popen(["/usr/bin/gnome-keyring-daemon", "--foreground", "--components=secrets",
        "--control-directory", str(control)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    try:
        while True:
            if daemon.poll() is not None:
                raise SynProtocolError("Private native credential service exited")
            result = subprocess.run(["/usr/bin/dbus-send", "--session", "--dest=org.freedesktop.DBus",
                "--type=method_call", "--print-reply", "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner",
                "string:org.freedesktop.secrets"], capture_output=True, timeout=2, check=False)
            if result.returncode == 0 and b"boolean true" in result.stdout:
                break
            if time.monotonic() >= deadline:
                raise SynProtocolError("Private native credential service did not become ready")
            time.sleep(0.05)
        unlock()
        return daemon
    except BaseException:
        _stop(daemon)
        raise


def _stop(child):
    if child is None:
        return
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
    child.wait(timeout=5)


def run(args):
    os.chdir(os.environ["HOME"])
    observed = require_scope()

    directory = args.state_dir
    if not directory.exists():
        private_directory(directory, create=True)
    with _agent_key_lock(directory, args.keyring_service, args.keyring_account + ".session"):
        return _run_locked(args, observed)


def require_scope():
    observed = scope()
    if (os.getuid() == 0 or not observed["capabilities_zero"] or not observed["no_new_privileges"]
            or not observed["internet_socket_creation_denied"] or not observed["root_read_only"]
            or sorted(observed["uid_map"]) != [[0, 0, 1], [os.getuid(), os.getuid(), 1]]):
        raise SynProtocolError("Agent session requires the qualified restricted Linux sandbox profile")
    return observed


def _run_locked(args, observed):
    directory = args.state_dir
    daemon, child = None, None
    try:
        daemon = _start_keyring()
        config_path = directory / "agent.json"
        created = None
        if args.new_identity:
            created = device.initialize(directory, service=args.keyring_service, account=args.keyring_account,
                expected_operator=args.expected_operator, network_id=args.network_id, group_id=args.group_id,
                group_epoch=args.group_epoch, approved_profile=args.approved_device_profile)
        config, config_sha = device.load_config(config_path)
        if (config["keyring_service"] != args.keyring_service or config["keyring_account"] != args.keyring_account
                or config["expected_operator"] != args.expected_operator or config["network_id"] != args.network_id
                or config["group_id"] != args.group_id or config["group_epoch"] != args.group_epoch
                or config["device_profile"] != args.approved_device_profile):
            raise SynProtocolError("Retained Agent configuration differs from selected deployment policy")
        _output({"profile": "syn.agent-session/1", "created": created, "configuration_sha256": config_sha, "scope": observed})
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin.buffer, selectors.EVENT_READ)
            while True:
                if daemon.poll() is not None or child is not None and child.poll() is not None:
                    raise SynProtocolError("Owned Agent node or native credential service exited; deployment fenced")
                if not selector.select(0.25):
                    continue
                raw = sys.stdin.buffer.readline(65538)
                if not raw:
                    return
                if len(raw) > 65537 or not raw.endswith(b"\n"):
                    raise SynProtocolError("Private enrollment frame exceeds its bound")
                frame = exact(parse(raw[:-1]), {"operation", "payload"}, "private enrollment frame")
                op, payload = frame["operation"], frame["payload"]
                if child is not None and op not in {"restart", "lock"}:
                    raise SynProtocolError("Running Agent enrollment is immutable")
                if op == "offer":
                    value = enrollment.create_offer(config, directory, payload)
                elif op == "choose":
                    value = enrollment.choose(config, directory, payload)
                elif op == "prepare":
                    value = enrollment.prepare(config, directory, payload, now=int(time.time()))
                elif op == "adopt":
                    exact(payload, {"grant", "request", "envelope"}, "encrypted key handoff")
                    value = adoption.adopt_selected_key(grant_packet=payload["grant"], request_packet=payload["request"],
                        envelope=payload["envelope"], service=config["keyring_service"], account=config["keyring_account"],
                        lock_dir=directory, expected_master_hash=bytes.fromhex(config["expected_operator"]), now=int(time.time()))
                elif op == "adopt-group":
                    from .group import adopt_group_key
                    exact(payload, {"grant", "request", "group"}, "initial group handoff")
                    value = adopt_group_key(config, directory, grant_packet=payload["grant"], request_packet=payload["request"],
                                             envelope=payload["group"], now=int(time.time()))
                elif op == "activate":
                    value = service.activate(config_path, payload)
                elif op == "lock":
                    if payload is not None:
                        raise SynProtocolError("Private keystore lock takes no caller-controlled parameters")
                    import secretstorage
                    secretstorage.Collection(secretstorage.dbus_init()).lock()
                    value = {"profile": "syn.agent-keystore-locked/1", "locked": True}
                elif op in {"serve", "restart"}:
                    if op == "restart" and child is None:
                        raise SynProtocolError("Restart requires the session's existing owned node")
                    if payload is not None:
                        raise SynProtocolError("Node lifecycle operation takes no caller-controlled parameters")
                    _stop(child)
                    child = subprocess.Popen([sys.executable, "-I", "-B", "-m", "syn.agent", "serve", "--config", str(config_path)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    server = service.AgentServer(config_path)
                    deadline = time.monotonic() + 10
                    while True:
                        if child.poll() is not None:
                            raise SynProtocolError("Owned Agent node refused startup")
                        try:
                            value = agent_status(socket_path=directory / "agent.sock", owner_uid=os.getuid(),
                                expected_hash=server.expected_hash, expected_operator=config["expected_operator"],
                                expected_version=service.runtimeContract().runtime_version, configuration_sha256=config_sha,
                                assignment_sha256=server.assignment_sha, expected_group=config["group_id"], expected_epoch=config["group_epoch"])
                            if value["status"]["pid"] != child.pid:
                                raise SynProtocolError("Agent endpoint belongs to a different process")
                            break
                        except (OSError, SynProtocolError):
                            if time.monotonic() >= deadline:
                                raise SynProtocolError("Owned Agent node did not become ready") from None
                            time.sleep(0.05)
                else:
                    raise SynProtocolError("Unknown private enrollment operation")
                _output(value)
    finally:
        _stop(child)
        _stop(daemon)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--keyring-service", required=True)
    parser.add_argument("--keyring-account", required=True)
    parser.add_argument("--expected-operator", required=True)
    parser.add_argument("--network-id", required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--group-epoch", type=int, required=True)
    parser.add_argument("--approved-device-profile", required=True)
    parser.add_argument("--new-identity", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
        return 0
    except Exception:
        print("Syn private Agent session refused; deployment fenced", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
