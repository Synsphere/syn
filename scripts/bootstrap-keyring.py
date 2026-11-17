"""Start/unlock an encrypted native Secret Service in the current D-Bus session.

Only a passphrase entered on the controlling terminal is accepted. No automatic
empty/default passphrase, private-key export, or headless plaintext fallback.
"""
from __future__ import annotations

from getpass import getpass
import os
from pathlib import Path
import subprocess
import sys


def service_running() -> bool:
    result = subprocess.run(
        ["dbus-send", "--session", "--print-reply",
         "--dest=org.freedesktop.DBus", "/org/freedesktop/DBus",
         "org.freedesktop.DBus.NameHasOwner", "string:org.freedesktop.secrets"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    return result.returncode == 0 and "boolean true" in result.stdout


def unlock() -> None:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise RuntimeError("D-Bus session address is missing")
    if service_running():
        # A pre-existing desktop session may already provide an unlocked store;
        # Syn's existing keystore operations still verify access on use.
        return
    if not sys.stdin.isatty():
        raise RuntimeError("A terminal is required to unlock the native keyring")

    exists = (Path.home() / ".local/share/keyrings/login.keyring").exists()
    message = "Unlock existing Syn/OS keyring: " if exists else "Create an encrypted Linux keyring passphrase: "
    password = getpass(message)
    if len(password) < (1 if exists else 12):
        raise RuntimeError("A nonempty existing password or a new passphrase of at least 12 characters is required")
    if not exists and password != getpass("Confirm new keyring passphrase: "):
        raise RuntimeError("Keyring passphrases did not match")

    try:
        result = subprocess.run(
            ["gnome-keyring-daemon", "--unlock", "--components=secrets"],
            input=password + "\n", text=True, capture_output=True,
            timeout=20, check=False,
        )
    finally:
        password = ""  # Best effort; Python cannot guarantee zeroization.
    if result.returncode != 0 or not service_running():
        raise RuntimeError("GNOME Keyring could not expose an unlocked Secret Service")


def main() -> int:
    try:
        unlock()
    except (OSError, subprocess.SubprocessError, RuntimeError, EOFError, KeyboardInterrupt) as error:
        print(f"Syn: secure native keyring setup failed: {error}", file=sys.stderr)
        return 1
    print("Syn: native Secret Service is ready", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
