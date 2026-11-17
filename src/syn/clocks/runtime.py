"""Clock jobs use the existing ProcessScheduler and its node ownership lease."""
from __future__ import annotations

from functools import partial
import logging
from uuid import uuid4

from sqlalchemy import update
from syn.positions.types import digest
from syn.recurrence import schedules
from .config import ClockConfig
from .store import ClockStore

NTP, LOCAL, GPSD = "clock-ntp", "clock-local", "clock-gpsd"


def schedule_prefix(node_ref):
    return "clock-"+digest(node_ref)[:32]+"-"


class ClockRuntime:
    def __init__(self, sessions, database_url, node_ref, config=None):
        self.sessions, self.database_url, self.nodeRef = sessions, database_url, node_ref
        self.config = config or ClockConfig()
        self.sessionId = str(uuid4())
        self.store = ClockStore(sessions)

    def handlers(self):
        from .jobs import run_gpsd, run_local, run_ntp
        args = (self.database_url, self.config.to_dict(), self.nodeRef)
        return {NTP: partial(run_ntp, *args), LOCAL: partial(run_local, *args), GPSD: partial(run_gpsd, *args)}

    def prepare(self, scheduler):
        if self.store.current(self.nodeRef) is not None:
            self.store.verify(self.nodeRef)
        result = self.store.begin(self.nodeRef, self.sessionId, self.config)
        keys = []
        def every(suffix, handler, args, seconds):
            key = schedule_prefix(self.nodeRef)+digest(suffix)[:32]
            keys.append(key)
            scheduler.every(key, handler, {"sessionId": self.sessionId, **args}, seconds)
        # Unconditional, independently scheduled NTP even with a better GPS/PHC.
        for host in self.config.ntpServers:
            every("ntp:"+host, NTP, {"server": host}, self.config.ntpSeconds)
        every("local", LOCAL, {}, self.config.pollSeconds)
        for host in self.config.gpsdHosts:
            every("gpsd:"+host, GPSD, {"address": host}, self.config.pollSeconds)
        with self.sessions.begin() as session:
            session.execute(update(schedules).where(
                schedules.c.schedulerKey == scheduler.schedulerKey,
                schedules.c.scheduleKey.startswith(schedule_prefix(self.nodeRef)),
                ~schedules.c.scheduleKey.in_(keys)).values(enabled=False))
        logging.getLogger("uvicorn.error").info(
            "Syn clock initialized: node=%s offset=0ms precision=unknown ember=%s NTP=%s",
            self.nodeRef, result["emberRef"], ",".join(self.config.ntpServers))
