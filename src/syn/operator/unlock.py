"""Explicit private terminal unlock of the native OS credential service.

This operation creates no Syn identity. Background daemons never unlock a
collection or prompt implicitly; only this human-invoked command may do so.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import subprocess
import sys
import warnings

from syn_client.operator_protocol import SynProtocolError
from .custody import _native_backend, backend_profile, private_directory


def _private_terminal():
    try:
        terminal = open("/dev/tty", "r+", encoding="utf-8")
    except OSError:
        raise SynProtocolError("Unlock requires a private local controlling terminal") from None
    # /dev/tty is a root-owned device alias. The kernel selects this process's
    # controlling terminal; foreground group ownership is the relevant check.
    if not terminal.isatty() or os.tcgetpgrp(terminal.fileno()) != os.getpgrp():
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
            # Native GNOME control IPC delivers the password to the existing
            # user service. It never appears in argv, env, stdout or receipts.
            try:
                result = subprocess.run(["/usr/bin/gnome-keyring-daemon", "--unlock", "--control-directory", str(control)],
                                        input=password.encode("utf-8"), stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=30, check=False)
            finally:
                del password
            if result.returncode:
                raise SynProtocolError("Native credential-service unlock was refused")
    _native_backend().get_password("syn.operator.readiness", "absent-probe")
    return {"profile": "syn.native-keystore-status/1", "custody": profile,
            "ready": True, "identity_created": False}
