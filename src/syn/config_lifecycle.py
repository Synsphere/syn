"""Non-destructive configuration checks and owner-only recovery snapshots.

Syn TOML is an *override file*: new capability defaults are supplied by the
runtime, not by rewriting private user configuration during each upgrade.
An invalid user-edited file is never automatically replaced. Operators can
explicitly restore the last configuration that passed Syn's full loader.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time


class ConfigRecoveryError(RuntimeError):
    """Recovery was unsafe, unavailable, or unnecessary; no config was replaced."""


def _source(path: str | Path) -> Path:
    return Path(path).expanduser().absolute()


def _private_dir(path: Path) -> Path:
    """Require an owner-only directory without changing existing permissions."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_mode & 0o077):
        raise ConfigRecoveryError(f"Unsafe config recovery directory: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfigRecoveryError(f"Config recovery directory has another owner: {path}")
    return path


def _recovery_dir(path: Path, *, state_root: Path | None = None) -> Path:
    if state_root is None:
        xdg = os.environ.get("XDG_STATE_HOME")
        state_root = Path(xdg).expanduser() if xdg else Path.home() / ".local/state"
        state_root = state_root / "syn/config-recovery"
    key = hashlib.sha256(os.fsencode(str(path))).hexdigest()
    return _private_dir(_private_dir(state_root) / key)


def _regular_bytes(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ConfigRecoveryError(f"Configuration must be a non-hardlinked regular file: {path}")
        data = stream.read()
        after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns
        ):
            raise ConfigRecoveryError("Configuration changed during validation")
        return data, after


def _unchanged(path: Path, expected: bytes, before: os.stat_result) -> bool:
    try:
        content, after = _regular_bytes(path)
    except OSError:
        return False
    return content == expected and (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_mtime_ns
    )


def _atomic_private(path: Path, data: bytes, *, replace: bool) -> None:
    """fsync private temporary bytes; atomically publish in the same directory."""
    fd, temporary = tempfile.mkstemp(prefix=".syn-config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise ConfigRecoveryError(f"Protected recovery copy already exists: {path}") from exc
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def _locked(directory: Path):
    # The Windows launcher executes syn-server *inside WSL*. Locking the backup
    # directory also serialises competing Syn launchers for the same config.
    import fcntl

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory / ".lock", flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o077):
            raise ConfigRecoveryError("Unsafe configuration recovery lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def remember_good_config(path: str | Path, *, state_root: Path | None = None) -> Path:
    """Remember exactly the config bytes that successfully passed full loading.

    This never writes to the user's config file or changes explicit opt-outs.
    A snapshot is updated only after validation, with permission 0600.
    """
    from .config import loadConfig

    source = _source(path)
    data, fingerprint = _regular_bytes(source)
    loadConfig(source)  # Full semantic validation, not just TOML syntax.
    directory = _recovery_dir(source, state_root=state_root)
    with _locked(directory):
        if not _unchanged(source, data, fingerprint):
            raise ConfigRecoveryError("Configuration changed after validation")
        backup = directory / "last-good.toml"
        if backup.exists():
            prior, _ = _regular_bytes(backup)
            if prior == data:
                return backup
        _atomic_private(backup, data, replace=True)
        return backup


def restore_good_config(path: str | Path, *, state_root: Path | None = None) -> Path:
    """Explicitly restore a validated snapshot while preserving the bad bytes.

    Never overwrites a currently valid config, missing file, symlink, or a
    config that changes between preflight and atomic replacement.
    Returns the private path to the preserved invalid file.
    """
    from .config import loadConfig

    if os.name != "posix":
        raise ConfigRecoveryError("Config recovery must run in the Linux/WSL Syn environment")
    source = _source(path)
    current, fingerprint = _regular_bytes(source)
    try:
        loadConfig(source)
    except (ValueError, OSError):
        pass
    else:
        raise ConfigRecoveryError("Current configuration is valid; refusing to replace it")

    directory = _recovery_dir(source, state_root=state_root)
    with _locked(directory):
        backup = directory / "last-good.toml"
        try:
            previous, _ = _regular_bytes(backup)
            loadConfig(backup)
        except (OSError, ValueError) as exc:
            raise ConfigRecoveryError("No validated last-known-good configuration is available") from exc
        if not _unchanged(source, current, fingerprint):
            raise ConfigRecoveryError("Configuration changed during recovery; aborting")
        digest = hashlib.sha256(current).hexdigest()[:12]
        quarantine = directory / f"invalid-{time.time_ns()}-{digest}.toml"
        _atomic_private(quarantine, current, replace=False)
        # Check once more after copying the damaged file, before publication.
        if not _unchanged(source, current, fingerprint):
            raise ConfigRecoveryError(f"Configuration changed; invalid copy retained at {quarantine}")
        _atomic_private(source, previous, replace=True)
        return quarantine


def configuration_report(path: str | Path) -> dict:
    """Effective policy only; never return connection strings or secret values.

    This is a static configuration check, not proof of hardware availability
    or successful networking/DB/keystore handshakes.
    """
    from .config import loadConfig

    config = loadConfig(path)
    p2p = config.p2p
    return {
        "configuration": "valid",
        "source": str(_source(path)),
        "services": {
            "database": "configured-not-probed",
            "operator": "not-probed",
            "awareness": "not-probed",
            "bluetooth": "enabled-not-probed" if config.bluetooth.enabled else "disabled",
        },
        "p2p": {
            "enabled": p2p.enabled,
            "amino": p2p.aminoEnabled,
            "dns4": p2p.dns4Enabled,
            "mdns": p2p.mdnsEnabled,
            "quic": p2p.quicEnabled,
            "upnp": p2p.upnpEnabled,
            "webrtc": p2p.webRtcEnabled,
            "webtransport": p2p.webTransportEnabled,
            "websocket": p2p.webSocketEnabled,
        },
        "note": "Configuration validated; runtime availability has not been tested",
    }
