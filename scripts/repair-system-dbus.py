"""Restore the exact public D-Bus runtime mode affected by Syn's old umask.

The command accepts no paths or configuration overrides. It never changes
D-Bus policy, the socket, or any Syn private directory. Unexpected state is
reported for administrator inspection and leaves Bluetooth optional.
"""
from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import stat
import sys


DIRECTORY = Path("/run/dbus")


class RepairRefused(RuntimeError):
    pass


def repair_directory(path: Path, *, owner_uid: int, owner_gid: int) -> str:
    """Repair one already-existing directory; explicit arguments allow fixtures."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return "absent"
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if mode == 0o755:
            return "unchanged"
        if mode != 0o700:
            raise RepairRefused(f"mode {mode:04o} is not the known 0700 bootstrap issue")
        if (before.st_uid, before.st_gid) != (owner_uid, owner_gid):
            raise RepairRefused("owner/group do not match the system messagebus account")
        # chmod can expand an extended ACL's mask. Never adjust an administrator's
        # access/default ACL as a side effect of this narrowly targeted repair.
        names = os.listxattr(descriptor)
        if {"system.posix_acl_access", "system.posix_acl_default"}.intersection(names):
            raise RepairRefused("an explicit filesystem ACL requires administrator review")
        current = os.fstat(descriptor)
        entry = os.stat(path, follow_symlinks=False)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_uid, item.st_gid)
        if signature(current) != signature(before) or signature(entry) != signature(before):
            raise RepairRefused("the runtime directory changed during inspection")
        os.fchmod(descriptor, 0o755)
        return "repaired"
    finally:
        os.close(descriptor)


def main(argv=None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print("Usage: repair-system-dbus.py (fixed /run/dbus only)", file=sys.stderr)
        return 2
    try:
        user = pwd.getpwnam("messagebus")
        group = grp.getgrnam("messagebus")
        result = repair_directory(DIRECTORY, owner_uid=user.pw_uid, owner_gid=group.gr_gid)
    except (KeyError, OSError, RepairRefused) as error:
        print(f"Syn bootstrap: /run/dbus was not changed: {error}. "
              "Inspect its owner, permissions and ACLs as an administrator; Bluetooth remains optional.")
        return 0
    if result == "repaired":
        print("Syn bootstrap: Restored the messagebus-owned /run/dbus directory from 0700 to 0755; "
              "D-Bus policy and Syn private files are unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
