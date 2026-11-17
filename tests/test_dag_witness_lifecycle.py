"""Actual API lifespan with scheduler/P2P doubles; no OS/child-process qualification."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from syn.witnessing.config import WitnessConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('fail_sync', [False, True])
def test_operator_dag_preparation_precedes_scheduler_dispatch(monkeypatch, fail_sync):
    from syn.witnessing import runtime as witness_runtime
    calls = []
    class Scheduler:
        def __init__(self, *args, **kwargs):
            self.schedulerKey = kwargs['schedulerKey']
            self.fault = None
        async def start(self, prepare=None):
            calls.append('lease')
            prepare(self)
            calls.append('pump')
        def every(self, *args):
            calls.append('schedule')
        async def stop(self):
            calls.append('scheduler-stop')
    class P2P:
        def __init__(self, *args):
            pass
        async def start(self):
            calls.append('p2p-start')
        async def stop(self):
            calls.append('p2p-stop')
        def status(self):
            return {'running': True}
    class Operator:
        server = types.SimpleNamespace(expected_hash='ab' * 32)
        def start(self):
            calls.append('operator-start')
        def close(self):
            calls.append('operator-stop')
        def check(self):
            pass
    position = types.SimpleNamespace(nodeRef='syn://thing/test-lifecycle',
        config=types.SimpleNamespace(maxConcurrentJobs=4), prepare=lambda _: calls.append('position'))
    for name, attrs in {
        'syn.scheduler': {'ProcessScheduler': Scheduler, 'JobHandler': object, 'UnknownHandlerError': ValueError},
        'syn.p2p': {'Libp2pNode': P2P},
        'syn.controllers': {'primitiveRouters': []},
    }.items():
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(witness_runtime, 'prepare', lambda *a: calls.append('witness-prepare'))
    monkeypatch.setattr(witness_runtime, 'check_local_jobs', lambda *a: None)
    spec = importlib.util.spec_from_file_location('syn._witness_lifespan_test', ROOT / 'src/syn/api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def sync(**kwargs):
        calls.append('dag-sync')
        if fail_sync:
            raise ValueError('anchor mismatch')
    app = module.createApp(sessionmaker(), {'operator-dag-sync': sync}, communicationTokens={},
        positionRuntime=position, operatorRuntime=Operator(), witnessConfig=WitnessConfig())
    if fail_sync:
        with pytest.raises(ValueError, match='anchor mismatch'):
            with TestClient(app):
                pass
        assert 'pump' not in calls and 'p2p-start' not in calls
    else:
        with TestClient(app) as client:
            assert client.get('/p2p/status').status_code == 200
        assert calls[:7] == ['operator-start', 'lease', 'position', 'dag-sync', 'schedule', 'witness-prepare', 'pump']
        assert calls.count('dag-sync') == 1
    assert 'operator-stop' in calls and 'scheduler-stop' in calls


def test_failed_local_integrity_job_fences_but_disabled_peer_does_not(tmp_path):
    from syn.recurrence import metadata, schedules, ensure_schedule
    from syn.witnessing.runtime import check_local_jobs
    engine = create_engine('sqlite:///' + str(tmp_path / 'health.db'))
    try:
        metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        job_id = uuid4()
        with sessions.begin() as s:
            s.execute(text('ALTER TABLE jobs ADD COLUMN status VARCHAR(32)'))
            s.execute(text('INSERT INTO jobs (id,status) VALUES (:id,\'failed\')'), {'id': job_id.hex})
            ensure_schedule(s, 'witness-test', 'scope', 'operator-dag-witness', {}, 120)
            s.execute(schedules.update().values(currentJobId=job_id))
        with pytest.raises(RuntimeError, match='lost readiness'):
            check_local_jobs(sessions, 'scope')
        check_local_jobs(sessions, 'other-scope')
        with sessions.begin() as s:
            s.execute(schedules.update().values(enabled=False))
        check_local_jobs(sessions, 'scope')
    finally:
        engine.dispose()
