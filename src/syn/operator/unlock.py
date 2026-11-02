"""Explicit private terminal unlock of the native OS credential service.

This operation creates no Syn identity. Background daemons never unlock a
collection or prompt implicitly; only this human-invoked command may do so.
"""
from __future__ import annotations

import getpass
import io
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import time
import warnings

from syn_client.operator_protocol import SynProtocolError
from .custody import _native_backend, backend_profile, private_directory


def _private_terminal():
    try:
        # BufferedRandom (ordinary text "r+") requires seeking and refuses
        # terminals. FileIO keeps the native duplex descriptor unbuffered.
        terminal = io.TextIOWrapper(io.FileIO("/dev/tty", "r+"),
                                    encoding="utf-8", write_through=True)
    except OSError:
        raise SynProtocolError("Unlock requires a private local controlling terminal") from None
    # /dev/tty is a root-owned device alias. The kernel selects this process's
    # controlling terminal; foreground group ownership is the relevant check.
    try:
        foreground = terminal.isatty() and os.tcgetpgrp(terminal.fileno()) == os.getpgrp()
    except OSError:
        foreground = False
    if not foreground:
        terminal.close()
        raise SynProtocolError("Unlock terminal is not owned by this OS account")
    return terminal


def _password(prompt: str, terminal) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(prompt, stream=terminal)
    if not value or len(value.encode("utf-8")) > 1024 or "\0" in value or "\n" in value:
        raise SynProtocolError("Native keystore requires a nonempty bounded private password")
    return value


def _unlock_gnome(control: Path, password: str) -> None:
    # Native GNOME PAM/control protocol, not daemon startup. See:
    # https://github.com/GNOME/gnome-keyring/blob/master/daemon/control/gkd-control-client.c
    # https://github.com/GNOME/gnome-keyring/blob/master/daemon/control/gkd-control-codes.h
    path = control / "control"
    before = path.lstat()
    if before.st_uid != os.getuid() or not stat.S_ISSOCK(before.st_mode):
        raise SynProtocolError("Native credential control endpoint has unsafe ownership or type")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(path))
        _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        after = path.lstat()
        if uid != os.getuid() or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise SynProtocolError("Native credential control peer differs from the selected endpoint")
        raw = password.encode("utf-8")
        # Credential byte, uint32 packet length, unlock opcode, bounded string.
        connection.sendall(b"\0" + struct.pack("!III", 12 + len(raw), 1, len(raw)) + raw)
        reply = bytearray()
        while len(reply) < 8:
            part = connection.recv(8 - len(reply))
            if not part:
                raise SynProtocolError("Native credential-service unlock response was truncated")
            reply.extend(part)
        if struct.unpack("!II", reply) != (8, 0):
            raise SynProtocolError("Native credential-service unlock was refused")


def unlock():
    profile = backend_profile()
    try:
        _native_backend().get_password("syn.operator.readiness", "absent-probe")
    except Exception:
        pass
    else:
        return {"profile": "syn.native-keystore-status/1", "custody": profile,
                "ready": True, "identity_created": False}
    with _private_terminal() as terminal:
        if sys.platform == "darwin":
            result = subprocess.run(["/usr/bin/security", "unlock-keychain"],
                                    stdin=terminal, stdout=terminal, stderr=terminal, check=False)
            if result.returncode:
                raise SynProtocolError("Native Keychain unlock was refused")
        else:
            runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
            private_directory(runtime)
            control = runtime / "keyring"
            private_directory(control)
            if not (control / "control").exists():
                raise SynProtocolError("Start the installed native keyring user service first")
            import secretstorage
            connection = secretstorage.dbus_init()
            try:
                secretstorage.Collection(connection)
            except secretstorage.exceptions.ItemNotFoundException:
                fresh = True
            else:
                fresh = False
            password = _password("Native keystore password (private, no echo): ", terminal)
            if fresh and password != _password("Confirm new native keystore password: ", terminal):
                raise SynProtocolError("Native keystore confirmation differed")
            # Native control IPC delivers the password to the authenticated
            # existing user service. The daemon --unlock CLI starts another
            # daemon; it does not unlock an already running service.
            try:
                _unlock_gnome(control, password)
            finally:
                del password
    # Native control IPC can acknowledge before the D-Bus collection appears.
    # Only this explicit human operation waits; background reads still refuse.
    deadline = time.monotonic() + 5
    while True:
        try:
            _native_backend().get_password("syn.operator.readiness", "absent-probe")
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise SynProtocolError("Native keystore did not become ready after explicit unlock") from None
            time.sleep(0.1)
    return {"profile": "syn.native-keystore-status/1", "custody": profile,
            "ready": True, "identity_created": False}
