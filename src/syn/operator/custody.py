"""Native software keystore and one host-wide lock per operator authority.

Reconciles the transitional Phase44 custody boundary into canonical Syn. There
is no configurable fallback backend, private-key IPC or hardware-enclave claim.
"""
from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path
import re
import stat
import sys
import time
from contextlib import contextmanager

from syn_client.operator_protocol import SynProtocolError


def native_home() -> Path:
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)


def private_directory(path: Path, *, create: bool = False) -> None:
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise SynProtocolError("Operator directory must be canonical and absolute")
    for parent in path.parents:
        info = parent.stat()
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_uid not in {0, os.getuid()} or (info.st_mode & 0o022 and not sticky_root):
            raise SynProtocolError("Operator path has an untrusted writable ancestor")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SynProtocolError("Operator directory must be owned by this user and private")


def private_read(path: Path, maximum: int = 65536) -> bytes:
    private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or not 0 < info.st_size <= maximum):
            raise SynProtocolError("Operator file has unsafe ownership, size or links")
        raw = os.read(fd, maximum + 1)
        if len(raw) != info.st_size:
            raise SynProtocolError("Operator file changed during read")
        return raw
    finally:
        os.close(fd)


def backend_profile() -> str:
    if sys.platform == "linux":
        return "linux-secret-service"
    if sys.platform == "darwin":
        return "macos-keychain"
    raise SynProtocolError("Operator custody requires native Linux/macOS; Windows uses WSL")


def _native_backend() -> object:
    module, name = {
        "linux-secret-service": ("keyring.backends.SecretService", "Keyring"),
        "macos-keychain": ("keyring.backends.macOS", "Keyring"),
    }[backend_profile()]
    try:
        cls = getattr(importlib.import_module(module), name)
        backend = cls()
        if backend.priority <= 0:
            raise ValueError("unavailable")
        if sys.platform == "linux":
            return _NoninteractiveSecretService()
        if sys.platform == "darwin":
            # Background readiness must refuse a locked keychain rather than
            # cause an OS authentication dialog. Explicit unlock is separate.
            import ctypes
            security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            allow = security.SecKeychainSetUserInteractionAllowed
            allow.argtypes = [ctypes.c_ubyte]
            allow.restype = ctypes.c_int32
            if allow(0) != 0:
                raise ValueError("cannot disable implicit keychain prompts")
        return backend
    except Exception as exc:
        raise SynProtocolError("Native OS keystore is unavailable or locked") from exc


class _NoninteractiveSecretService:
    """Native API without keyring's implicit unlock/create-collection prompts."""
    @staticmethod
    def _collection():
        import secretstorage
        connection = secretstorage.dbus_init()
        # get_default_collection() CREATES a missing collection and prompts.
        # Collection() is read-only and raises when no default exists.
        collection = secretstorage.Collection(connection)
        if collection.is_locked():
            raise SynProtocolError("Native Secret Service collection is locked")
        return collection

    def get_password(self, service: str, account: str) -> str | None:
        collection = self._collection()
        matches = list(collection.search_items({"service": service, "username": account}))
        if len(matches) > 1:
            raise SynProtocolError("Native credential account has ambiguous duplicate entries")
        if not matches:
            return None
        if matches[0].is_locked():
            raise SynProtocolError("Native credential entry is locked")
        return matches[0].get_secret().decode("utf-8")

    def set_password(self, service: str, account: str, value: str) -> None:
        collection = self._collection()
        matches = list(collection.search_items({"service": service, "username": account}))
        if len(matches) > 1:
            raise SynProtocolError("Native credential account has ambiguous duplicate entries")
        collection.create_item("Syn operator " + service + "/" + account,
                               {"service": service, "username": account}, value.encode("utf-8"),
                               replace=True, content_type="text/plain; charset=utf-8")


def _entry(service: str, account: str) -> None:
    for value in (service, account):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", value):
            raise SynProtocolError("Invalid bounded native keystore reference")


@contextmanager
def _agent_key_lock(directory: Path, service: str, account: str):
    """Preserve the HD helper API, but never trust a caller-selected lock root.

    The OS account's fixed Syn lock directory serializes identical keystore
    accounts even when an attempted second installation selects another path.
    """
    import fcntl
    _entry(service, account)
    private_directory(directory)
    home = native_home()
    home_info = home.stat()
    if home_info.st_uid != os.getuid() or home_info.st_mode & 0o022:
        raise SynProtocolError("Native account home is writable by another principal")
    for part in (".local", "state", "syn", "authority-locks"):
        home /= part
        if not home.exists():
            home.mkdir(mode=0o700)
        # Existing .local may be public; the Syn-specific state is always private.
        info = home.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or home.is_symlink()):
            raise SynProtocolError("Unsafe native authority-lock parent")
        if part in {"syn", "authority-locks"}:
            private_directory(home)
    name = hashlib.sha256((service + "\0" + account).encode()).hexdigest()
    fd = os.open(home / (name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise SynProtocolError("Unsafe authority lock")
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SynProtocolError("Another process owns this operator authority") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)
