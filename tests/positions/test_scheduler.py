"""Real scheduling control flow with a deterministic process-executor stand-in.

These do not qualify asyncprocessscheduler's multiprocessing behaviour or PG
advisory locks. The PostgreSQL test module covers DB-specific contracts.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import importlib
import sys
import types
from uuid import uuid4

import pytest
from sqlalchemy import Column, DateTime, Enum, JSON, String, Uuid, create_engine, select
from sqlalchemy.orm import declarative_base, sessionmaker


@pytest.fixture
def scheduling(tmp_path,monkeypatch):
    Base=declarative_base()
    class Status(StrEnum):
        queued="queued";running="running";succeeded="succeeded";failed="failed";canceled="canceled"
    class Job(Base):
        __tablename__="jobs"
        id=Column(Uuid,primary_key=True)
        handler=Column(String,nullable=False)
        arguments=Column(JSON,nullable=False)
        status=Column(Enum(Status),nullable=False)
        result=Column(JSON)
        error=Column(String)
        createdAt=Column(DateTime(timezone=True),nullable=False)
        startedAt=Column(DateTime(timezone=True))
        finishedAt=Column(DateTime(timezone=True))
    # Only the persistence projection and process boundary are replaced.
    model=types.ModuleType("syn.models");model.Job=Job;model.JobStatus=Status
    monkeypatch.setitem(sys.modules,"syn.models",model)
    class Executor:
        def __init__(self,**kw):self.terminated=False
        def add(self,*,target,args):self.target,self.args=target,args
        async def run(self):
            await asyncio.sleep(.01)
            if self.terminated:raise RuntimeError("terminated")
            return [self.target(*self.args)]
        def terminate(self):self.terminated=True
    library=types.ModuleType("scheduler.Scheduler");library.Scheduler=Executor
    package=types.ModuleType("scheduler");package.__path__=[]
    monkeypatch.setitem(sys.modules,"scheduler",package)
    monkeypatch.setitem(sys.modules,"scheduler.Scheduler",library)
    # Loading the actual file avoids retaining models imported by another test.
    from pathlib import Path
    source=Path(__file__).resolve().parents[2]/"src/syn/scheduler.py"
    mod=types.ModuleType("syn.scheduler");mod.__package__="syn";mod.__file__=str(source)
    exec(compile(source.read_text(),str(source),"exec"),mod.__dict__)
    monkeypatch.setitem(sys.modules,"syn.scheduler",mod)
    from syn import recurrence
    engine=create_engine("sqlite:///"+str(tmp_path/"jobs.db"))
    Base.metadata.create_all(engine)
    recurrence.metadata.create_all(engine,tables=recurrence.RECURRENCE_TABLES)
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    yield mod,sessions,Job,Status,recurrence
    engine.dispose()


def test_due_no_overlap_and_fixed_delay(scheduling,now):
    mod,sessions,Job,Status,r=scheduling
    with sessions.begin() as s:
        r.ensure_schedule(s,"gps","node-a","poll",{},5,now=now)
        one=r.materialize_due(s,"node-a",now=now)
        assert len(one)==1
        assert r.materialize_due(s,"node-a",now=now+timedelta(hours=1))==[]
        job=s.get(Job,one[0]);job.status=Status.succeeded;job.finishedAt=now+timedelta(seconds=2)
    with sessions.begin() as s:
        assert r.materialize_due(s,"node-a",now=now+timedelta(seconds=6))==[]
        assert len(r.materialize_due(s,"node-a",now=now+timedelta(seconds=7)))==1


def test_backoff_and_coalescing(scheduling,now):
    mod,sessions,Job,Status,r=scheduling
    with sessions.begin() as s:
        r.ensure_schedule(s,"gps","node-a","poll",{},5,now=now)
        jobid=r.materialize_due(s,"node-a",now=now)[0]
        job=s.get(Job,jobid);job.status=Status.failed;job.finishedAt=now
    with sessions.begin() as s:
        assert r.materialize_due(s,"node-a",now=now+timedelta(seconds=4))==[]
        second=r.materialize_due(s,"node-a",now=now+timedelta(seconds=5))[0]
        job=s.get(Job,second);job.status=Status.failed;job.finishedAt=now+timedelta(seconds=5)
    with sessions.begin() as s:
        assert r.materialize_due(s,"node-a",now=now+timedelta(seconds=14))==[]
        assert len(r.materialize_due(s,"node-a",now=now+timedelta(days=1)))==1
        assert r.materialize_due(s,"node-a",now=now+timedelta(days=1))==[]


def test_schedule_identity_and_scope(scheduling,now):
    mod,sessions,Job,Status,r=scheduling
    with sessions.begin() as s:
        r.ensure_schedule(s,"gps","a","poll",{},5,now=now)
        assert r.materialize_due(s,"b",now=now)==[]
        with pytest.raises(ValueError):r.ensure_schedule(s,"gps","b","poll",{},5,now=now)


def test_jobs_execute_and_stop(scheduling):
    mod,sessions,Job,Status,r=scheduling
    async def scenario():
        manager=mod.ProcessScheduler(sessions,{"double":lambda value:2*value},schedulerKey="a")
        job=manager.add("double",{"value":4})
        await manager.start()
        for _ in range(60):
            result=manager.get(job.id)
            if result.status==Status.succeeded:break
            await asyncio.sleep(.02)
        assert result.result==8
        await manager.stop()
        assert not manager.running and manager._lease is None
    asyncio.run(scenario())


def test_restart_only_recovers_owned_jobs(scheduling,now):
    mod,sessions,Job,Status,r=scheduling
    ids=[]
    with sessions.begin() as s:
        for scope in ("a","b"):
            job=Job(id=uuid4(),handler="poll",arguments={},status=Status.running,createdAt=now,startedAt=now)
            s.add(job);s.flush();r.bind_job(s,job.id,scope);ids.append(job.id)
    manager=mod.ProcessScheduler(sessions,{"poll":lambda:None},schedulerKey="a")
    manager._recover()
    with sessions() as s:
        assert s.get(Job,ids[0]).status==Status.queued
        assert s.get(Job,ids[1]).status==Status.running
    assert manager.get(ids[1]) is None


def test_cancel_schedule_prevents_future_jobs(scheduling,now):
    mod,sessions,Job,Status,r=scheduling
    manager=mod.ProcessScheduler(sessions,{"poll":lambda:None},schedulerKey="a")
    manager.every("gps","poll",{},5)
    with sessions.begin() as s:
        ids=r.materialize_due(s,"a",now=datetime.now(UTC)+timedelta(seconds=1))
    asyncio.run(manager.cancelSchedule("gps"))
    assert manager.get(ids[0]).status==Status.canceled
    with sessions.begin() as s:
        assert r.materialize_due(s,"a",now=datetime.now(UTC)+timedelta(days=1))==[]


def test_default_scope_retains_legacy_jobs(scheduling,now):
    mod,sessions,Job,Status,r=scheduling
    with sessions.begin() as s:
        job=Job(id=uuid4(),handler="poll",arguments={},status=Status.queued,createdAt=now)
        s.add(job);s.flush();jobid=job.id
    assert mod.ProcessScheduler(sessions,{}).get(jobid) is not None
    assert mod.ProcessScheduler(sessions,{},schedulerKey="other").get(jobid) is None


def test_executor_construction_failure_is_a_failed_job(scheduling,monkeypatch):
    mod,sessions,Job,Status,r=scheduling
    def broken(**kwargs):raise RuntimeError("executor unavailable")
    monkeypatch.setattr(mod,"Scheduler",broken)
    async def scenario():
        manager=mod.ProcessScheduler(sessions,{"poll":lambda:None},schedulerKey="a")
        job=manager.add("poll",{})
        await manager.start()
        try:
            for _ in range(60):
                result=manager.get(job.id)
                if result.status==Status.failed:break
                await asyncio.sleep(.01)
            assert result.status==Status.failed
            assert "executor unavailable" in result.error
        finally:await manager.stop()
    asyncio.run(scenario())
