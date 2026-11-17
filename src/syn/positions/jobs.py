"""Process-safe handlers registered with Syn's existing Job execution service."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
import os
from sqlalchemy import select, text

from syn.recurrence import ensure_schedule, schedules
from .config import PositionConfig
from .providers import Device, discover, poll
from .schema import devices, nodes, insert_ignore
from .store import PositionStore
from .types import digest

DISCOVER = "position-discover"
POLL = "position-poll"


def schedule_key(nodeRef, suffix):
    return "position-"+digest([nodeRef, suffix])


@contextmanager
def sessions_for(url):
    from syn.database import createEngine, createSessionFactory
    engine = createEngine(url, migrate=False)
    try:
        yield createSessionFactory(engine)
    finally:
        engine.dispose()


def device_enabled(device, config):
    """Apply policy again at execution time, including already queued polls."""
    if device.provider == "serial":
        path = os.path.realpath(device.address)
        return (config.serialProbe and path not in {os.path.realpath(p) for p in config.excludedSerialPorts}
                and (not config.serialPorts or path in {os.path.realpath(p) for p in config.serialPorts}))
    if device.provider == "gpsd":
        return device.options.get("endpoint") in config.gpsdHosts
    if device.provider == "network-nmea":
        return device.address in config.networkNmea
    if device.provider == "ble-ln":
        return config.bluetoothLE
    return config.nativeServices


def run_discovery(databaseUrl, settings, nodeRef, *, schedulerKey):
    config = PositionConfig.from_dict(settings)
    found, report = discover(config)
    with sessions_for(databaseUrl) as sessions:
        with sessions.begin() as session:
            # Serialize inventory publication for this node, not the slow probes.
            session.execute(select(nodes).where(nodes.c.nodeRef == nodeRef).with_for_update()).one()
            old = {r["deviceId"]: r for r in session.execute(select(devices).where(devices.c.nodeRef == nodeRef)).mappings()}
            now = datetime.now(UTC)
            seen = set()
            for device in found:
                previous = old.get(device.id)
                if previous and device.provider == "serial" and previous["descriptor"]["options"].get("confirmed"):
                    device = replace(device, options=previous["descriptor"]["options"])
                seen.add(device.id)
                value = {"descriptor": device.to_dict(), "active": True, "lastSeen": now, "lastError": None}
                insert_ignore(session, devices, {"nodeRef": nodeRef, "deviceId": device.id, **value}, ["nodeRef", "deviceId"])
                session.execute(devices.update().where(devices.c.nodeRef == nodeRef, devices.c.deviceId == device.id).values(**value))
                interval = config.rescanSeconds if device.provider == "serial" and not device.options.get("confirmed") else config.pollSeconds
                ensure_schedule(session, schedule_key(nodeRef, device.id), schedulerKey, POLL,
                                {"deviceId": device.id, "schedulerKey": schedulerKey}, interval)
            successful = {r["provider"] for r in report if r["status"] in {"available", "no-devices", "candidates"}}
            for deviceId, previous in old.items():
                if deviceId in seen:
                    continue
                provider = previous["descriptor"]["provider"]
                probe_name = "gpsd:"+previous["descriptor"]["options"]["endpoint"] if provider == "gpsd" else provider
                # Temporary service/permission failure preserves retry schedules.
                # Successful enumeration with absence is an actual unplug signal.
                removed_configured = not device_enabled(Device.from_dict(previous["descriptor"]), config)
                truncated = any(r["status"] == "truncated" for r in report)
                if removed_configured or (probe_name in successful and not truncated):
                    session.execute(devices.update().where(devices.c.nodeRef == nodeRef, devices.c.deviceId == deviceId).values(active=False))
                    session.execute(schedules.update().where(schedules.c.scheduleKey == schedule_key(nodeRef, deviceId)).values(enabled=False))
            session.execute(nodes.update().where(nodes.c.nodeRef == nodeRef).values(probeReport=report))
    return {"candidates": len(found), "providers": report}


def run_poll(databaseUrl, settings, nodeRef, *, deviceId, schedulerKey, clockSettings=None, clockSessionId=None):
    config = PositionConfig.from_dict(settings)
    with sessions_for(databaseUrl) as sessions:
        with sessions() as session:
            row = session.execute(select(devices).where(devices.c.nodeRef == nodeRef, devices.c.deviceId == deviceId)).mappings().first()
            if row is None or not row["active"]:
                return {"status": "inactive", "readings": 0}
            device = Device.from_dict(row["descriptor"])
            if not device_enabled(device, config):
                return {"status": "disabled", "readings": 0}
        # A dedicated transaction lock serializes receivers across local Syn nodes.
        # OS serial exclusivity additionally protects against other programs.
        with sessions.begin() as guard:
            if guard.get_bind().dialect.name == "postgresql":
                from .runtime import host_fingerprint
                resource = "syn.receiver:"+digest([host_fingerprint(), device.claimedPorts or (device.provider, device.address)])
                if not guard.execute(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"), {"key": resource}).scalar_one():
                    return {"status": "busy", "readings": 0}
            try:
                readings, options = poll(device, config)
            except Exception as error:
                with sessions.begin() as session:
                    session.execute(devices.update().where(devices.c.nodeRef == nodeRef, devices.c.deviceId == deviceId)
                                    .values(lastError=type(error).__name__))
                # Do not put arbitrary hardware data or credentials in Job.error.
                raise RuntimeError(f"Position provider {device.provider} failed ({type(error).__name__})") from None
            store = PositionStore(sessions)
            counts = {}
            clock_store = clock_config = None
            if clockSettings is not None and clockSessionId is not None:
                from syn.clocks.config import ClockConfig
                from syn.clocks.store import ClockStore
                from syn.clocks.providers import from_position
                clock_store, clock_config = ClockStore(sessions), ClockConfig.from_dict(clockSettings)
            for reading in readings:
                if clock_store is not None and device.provider in {"serial", "gpsd", "network-nmea", "modemmanager", "ble-ln"}:
                    # Only receiver timecodes, never OS location timestamps, are UTC references.
                    clock_store.observe(nodeRef, clockSessionId, from_position(reading, clock_config), clock_config)
                result = store.observe(nodeRef, reading, now=datetime.now(UTC), maxAgeSeconds=config.maxAgeSeconds,
                    futureToleranceSeconds=config.futureToleranceSeconds, driftPerSecond=config.driftMetersPerSecond)
                counts[result["decision"]] = counts.get(result["decision"], 0)+1
            with sessions.begin() as session:
                updated = replace(device, options=options)
                session.execute(devices.update().where(devices.c.nodeRef == nodeRef, devices.c.deviceId == deviceId)
                    .values(descriptor=updated.to_dict(), lastError=None))
                if device.provider == "serial" and options.get("confirmed"):
                    session.execute(schedules.update().where(schedules.c.scheduleKey == schedule_key(nodeRef, deviceId),
                        schedules.c.schedulerKey == schedulerKey).values(intervalSeconds=config.pollSeconds))
            return {"status": "read", "readings": len(readings), "decisions": counts}
