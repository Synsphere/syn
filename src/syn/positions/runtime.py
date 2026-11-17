"""Startup wiring shared by API, operator and Agent nodes.

The native socket services have a synchronous lifecycle. Their owned event-loop
thread hosts the SAME ProcessScheduler used by the API, not another scheduler.
Private wallet state and existing signed readiness wire formats are unchanged.
"""
from __future__ import annotations
import asyncio
from dataclasses import replace
from functools import lru_cache, partial
import hashlib
import os
from pathlib import Path
import platform
import secrets
import stat
import subprocess
import threading
from uuid import UUID

from .bootstrap import bootstrap
from .config import PositionConfig
from .jobs import DISCOVER, POLL, run_discovery, run_poll, schedule_key
from .store import PositionStore
from .types import digest


@lru_cache(maxsize=1)
def host_fingerprint():
    for name in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(name).read_text().strip()
            if value:
                return hashlib.sha256(value.encode()).hexdigest()
        except OSError:
            continue
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                return digest(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            pass
    if platform.system() == "Darwin":
        try:
            value = subprocess.run(["sysctl", "-n", "kern.uuid"], capture_output=True, text=True,
                                   timeout=2, check=True).stdout.strip()
            if value:
                return digest(value)
        except (OSError, subprocess.SubprocessError):
            pass
    return digest(platform.node())


def installation_id(directory):
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory/"position-node-id"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_hex(16)+"\n")
            stream.flush()
            os.fsync(stream.fileno())
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Position node identity must be a regular non-symlink file")
    raw = path.read_text().strip()
    if len(raw) != 32 or UUID(hex=raw).hex != raw:
        raise ValueError("Invalid position node identity; refusing silent replacement")
    return raw


class PositionRuntime:
    def __init__(self, sessions, databaseUrl, config: PositionConfig, *, identity=None):
        self.sessions, self.databaseUrl, self.config = sessions, databaseUrl, config
        identity = identity or installation_id(config.stateDirectory)
        self.nodeRef = config.nodeRef or "syn://thing/node-"+digest([identity, host_fingerprint()])
        self.store = PositionStore(sessions)

    def handlers(self):
        # Credentials stay in process-local closures, never in persisted Job arguments.
        common = (self.databaseUrl, self.config.to_dict(), self.nodeRef)
        return {DISCOVER: partial(run_discovery, *common), POLL: partial(run_poll, *common)}

    def prepare(self, scheduler):
        # Do not compute an expensive timezone catalogue or reset existing beliefs on restart.
        current = self.store.current(self.nodeRef)
        if not any(x["spaceRef"] == "syn://thing/space-wgs84-geodetic" for x in current):
            self.store.bootstrap(self.nodeRef, bootstrap(self.config))
        else:
            self.store.verify(self.nodeRef)
            self.store.upgrade_geographic_type(self.nodeRef)
        scheduler.every(schedule_key(self.nodeRef, "discovery"), DISCOVER,
                        {"schedulerKey": scheduler.schedulerKey}, self.config.rescanSeconds)


def native_position_configured(directory):
    """Only enable PostgreSQL-backed positioning for explicitly configured nodes.

    Legacy native socket profiles must retain independent startup and socket
    semantics. Invalid explicit positioning configuration still fails readiness.
    """
    return bool(
        os.environ.get("SYN_POSITION_CONFIG")
        or os.environ.get("SYN_DATABASE_URL")
        or (Path(directory) / "position.toml").is_file()
    )


class NativePositionRuntime:
    def __init__(self, server):
        from syn.config import DatabaseConfig, SynConfig, loadConfig
        from syn.database import createEngine, createSessionFactory
        from syn.scheduler import ProcessScheduler
        explicit = os.environ.get("SYN_POSITION_CONFIG")
        candidates = [Path(explicit)] if explicit else [server.directory/"position.toml", Path("config.toml")]
        configPath = next((p for p in candidates if p.is_file()), None)
        if configPath is not None:
            config = loadConfig(configPath)
        elif os.environ.get("SYN_DATABASE_URL"):
            config = SynConfig(database=DatabaseConfig(os.environ["SYN_DATABASE_URL"], autoMigrate=False))
        else:
            raise ValueError("Native node positioning requires SYN_POSITION_CONFIG, adjacent position.toml, "
                             "or SYN_DATABASE_URL for a migrated PostgreSQL database")
        self.engine = createEngine(config.database.url, echo=config.database.echo,
                                   migrate=config.database.autoMigrate)
        try:
            settings = replace(config.position, stateDirectory=str(server.directory))
            self.runtime = PositionRuntime(createSessionFactory(self.engine), config.database.url, settings,
                                           identity=server.expected_hash)
            self.scheduler = ProcessScheduler(self.runtime.sessions, self.runtime.handlers(),
                schedulerKey="node-"+digest(self.runtime.nodeRef), maxConcurrentJobs=settings.maxConcurrentJobs)
        except BaseException:
            self.engine.dispose()
            raise
        self.ready, self.stopped = threading.Event(), threading.Event()
        self.fault = None
        self.thread = None

    async def _serve(self):
        try:
            await self.scheduler.start()
            self.ready.set()
            while not self.stopped.is_set() and self.scheduler.running:
                await asyncio.sleep(.1)
            if self.scheduler.fault:
                self.fault = self.scheduler.fault
        except BaseException as error:
            self.fault = error
            self.ready.set()
        finally:
            try:
                await self.scheduler.stop()
            except BaseException as error:
                self.fault = error
                self.ready.set()

    def start(self):
        try:
            self.runtime.prepare(self.scheduler)
            self.thread = threading.Thread(target=lambda: asyncio.run(self._serve()), name="syn-position-scheduler", daemon=True)
            self.thread.start()
            if not self.ready.wait(30):
                raise RuntimeError("Position scheduler did not become ready")
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.thread is not None and not self.thread.is_alive() and not self.stopped.is_set() and self.fault is None:
            self.fault = RuntimeError("Position scheduler thread exited unexpectedly")
        if self.fault is not None:
            raise RuntimeError("Position scheduler failed; node readiness is fenced") from self.fault

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(30)
            if self.thread.is_alive():
                raise RuntimeError("Position scheduler did not stop; database remains owned")
        self.engine.dispose()
