"""Process-safe clock handlers; failures are observations, not lost telemetry."""
from __future__ import annotations

from syn.positions.jobs import sessions_for
from .config import ClockConfig
from .store import ClockStore
from .types import ClockReading
from . import ntp, providers


def active(store, node_ref, session_id):
    current = store.current(node_ref)
    return current is not None and current["sessionId"] == session_id


def run_ntp(database_url, settings, node_ref, *, sessionId, server):
    config = ClockConfig.from_dict(settings)
    if server not in config.ntpServers:
        return {"status": "disabled"}
    with sessions_for(database_url) as sessions:
        store = ClockStore(sessions)
        if not active(store, node_ref, sessionId):
            return {"status": "obsolete-session"}
        try:
            reading = ntp.poll(server, config.timeoutSeconds, step_threshold_ms=config.stepThresholdMs)
        except (OSError, ValueError) as error:
            # Do not copy arbitrary network payloads or credentials into ledger text.
            reading = ClockReading.failure("ntp:"+server, "ntp", type(error).__name__)
        result = store.observe(node_ref, sessionId, reading, config)
        return {"status": "sampled" if reading.valid else "source-failed", "eventRef": result.get("eventRef")}


def run_local(database_url, settings, node_ref, *, sessionId):
    config = ClockConfig.from_dict(settings)
    with sessions_for(database_url) as sessions:
        store = ClockStore(sessions)
        if not active(store, node_ref, sessionId):
            return {"status": "obsolete-session"}
        count = 0
        for reading in providers.local(config):
            store.observe(node_ref, sessionId, reading, config)
            count += 1
        return {"status": "sampled", "readings": count}


def run_gpsd(database_url, settings, node_ref, *, sessionId, address):
    config = ClockConfig.from_dict(settings)
    if address not in config.gpsdHosts:
        return {"status": "disabled"}
    with sessions_for(database_url) as sessions:
        store = ClockStore(sessions)
        if not active(store, node_ref, sessionId):
            return {"status": "obsolete-session"}
        count = 0
        # Catch transport failures only. A persistence error MUST fail the job.
        iterator = iter(providers.gpsd(address, config))
        while True:
            try:
                reading = next(iterator)
            except StopIteration:
                break
            except (OSError, ValueError) as error:
                reading = ClockReading.failure("gpsd:"+address, "gpsd-pps", type(error).__name__)
                store.observe(node_ref, sessionId, reading, config)
                count += 1
                break
            store.observe(node_ref, sessionId, reading, config)
            count += 1
        return {"status": "sampled", "readings": count}
