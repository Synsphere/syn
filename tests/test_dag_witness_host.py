"""Host independently checks append-only proofs; DPAPI IO itself is not emulated."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host-custody'))
from syn_host_custody.core import Authority, DAG_ANCHOR_SUFFIX
from syn_host_custody.broker import WindowsVault
from syn_host_custody.merkle import verify_prefix
from syn.operator import dag, dag_proofs, dag_evidence
from syn.witnessing.config import Peer
from syn.witnessing.anchors import CustodyAnchor
from test_dag_witnessing import Source


class PublicVault:
    def __init__(self, local):
        self.local, self.slots = local, {}
    def identity(self, _):
        return b'x' * 32, {'base_public_key_hash': self.local.pin, 'base_public_key': self.local.public.hex()}
    def get_slot(self, service, slot):
        return self.slots.get((service, slot))
    def set_slot(self, service, slot, value):
        self.slots[service, slot] = value


@pytest.mark.parametrize('size', [2, 3, 4, 5, 9, 16, 17, 32, 65, 129])
def test_host_verifier_agrees_with_runtime_for_every_prefix(size):
    leaves = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(size)]
    for older in range(1, size + 1):
        path = dag_proofs.consistency(leaves, older)
        old_root, new_root = dag_proofs.root(leaves[:older]), dag_proofs.root(leaves)
        assert verify_prefix(older, size, old_root, new_root, path)
        assert not verify_prefix(older, size, old_root, 'ff' * 32, path)


def test_local_anchor_rejects_valid_signature_with_rewritten_leaves():
    local = Source()
    vault = PublicVault(local)
    broker = Authority(vault, approve=lambda _: True)
    old = local.grow(2)
    common = {'op': 'dag_anchor', 'service': 'syn.operator', 'account': 'primary',
              'pin': local.pin, 'action': 'advance'}
    broker.request({**common, 'checkpoint': old, 'proof': []})
    fork = local.fork(5)
    bad = fork.checkpoint()
    bad['body']['previous_checkpoint'] = dag_evidence.checkpoint_digest(old)
    bad['signature'] = local.key.sign(dag.CHECKPOINT_DOMAIN + dag.canonical(bad['body'])).hex()
    with pytest.raises(ValueError, match='prefix proof'):
        broker.request({**common, 'checkpoint': bad, 'proof': dag_proofs.consistency(fork.leaves, 2)})
    assert json.loads(vault.slots['syn.operator', 'primary' + DAG_ANCHOR_SUFFIX]) == old


def test_host_witness_pins_scope_and_prevents_generic_slot_bypass():
    local, remote = Source(), Source()
    vault = PublicVault(local)
    broker = Authority(vault)
    old = remote.grow(2)
    req = {'op': 'dag_witness_anchor', 'service': 'syn.operator', 'account': 'primary',
           'pin': local.pin, 'peer': remote.pin, 'network_id': 'syn', 'action': 'advance'}
    broker.request({**req, 'checkpoint': old, 'proof': []})
    new = remote.grow(7)
    broker.request({**req, 'checkpoint': new, 'proof': dag_proofs.consistency(remote.leaves, 2)})
    with pytest.raises(ValueError, match='rewind'):
        broker.request({**req, 'checkpoint': old, 'proof': []})
    with pytest.raises(ValueError, match='network'):
        broker.request({**req, 'action': 'read', 'network_id': 'other'})
    for operation in ['get', 'set']:
        with pytest.raises(ValueError):
            broker.request({'op': operation, 'service': 'syn.operator', 'account': 'slot',
                'slot': 'primary.operator.witness-' + remote.pin + '.v1', 'value': '{}'})
    assert broker.request({**req, 'action': 'read'}) == new


def test_wsl_bridge_sends_proof_and_checks_host_ack(monkeypatch):
    from types import SimpleNamespace
    from syn.operator import host_custody
    local, remote = Source(), Source()
    broker = Authority(PublicVault(local))
    monkeypatch.setattr(host_custody, 'enabled', lambda: True)
    monkeypatch.setattr(host_custody, 'request', broker.request)
    server = SimpleNamespace(expected_hash=local.pin,
        config={'keyring_service': 'syn.operator', 'keyring_account': 'primary'})
    anchor = CustodyAnchor(server, Peer(remote.pin, 'syn', 'https://peer'))
    old = remote.grow(2)
    anchor.advance(old, [])
    assert anchor.read() == old
    newer = remote.grow(4)
    anchor.advance(newer, dag_proofs.consistency(remote.leaves, 2))
    assert anchor.read() == newer
    monkeypatch.setattr(host_custody, 'request', lambda _: {'sequence': 5, 'digest': 'ab' * 32})
    with pytest.raises(ValueError, match='acknowledgement'):
        anchor.advance(newer, [])


def test_vault_failed_write_cannot_be_acknowledged_from_memory():
    vault = WindowsVault.__new__(WindowsVault)
    vault.data = {'version': 1, 'identities': {}, 'slots': {'test/value': 'old'}, 'approvals': []}
    before = deepcopy(vault.data)
    def fail():
        raise OSError('disk full')
    vault._persist = fail
    with pytest.raises(OSError, match='disk full'):
        vault.set_slot('test', 'value', 'new')
    assert vault.data == before
    with pytest.raises(OSError, match='restart'):
        vault.get_slot('test', 'value')


def test_migration_sql_and_single_alembic_head():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    path = ROOT / 'src/syn/alembic/versions/20261117_0020_dag_witnessing.py'
    spec = importlib.util.spec_from_file_location('witness_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    module.op = Operations(context)
    module.upgrade()
    sql = output.getvalue()
    assert 'CREATE TABLE "operatorDagWitnessRecords"' in sql
    assert 'BEFORE UPDATE OR DELETE' in sql and 'BEFORE TRUNCATE' in sql
    config = Config()
    config.set_main_option('script_location', str(ROOT / 'src/syn/alembic'))
    assert ScriptDirectory.from_config(config).get_current_head() == '20261118_0021'
    with pytest.raises(RuntimeError, match='archival'):
        module.downgrade()


def test_scheduler_reconciles_removed_peers_and_queued_work(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from syn.witnessing import runtime
    from syn.witnessing.config import WitnessConfig
    from syn.recurrence import metadata as recurrence_meta, ensure_schedule, schedules
    from sqlalchemy import select
    engine = create_engine('sqlite:///' + str(tmp_path / 'jobs.db'))
    try:
        recurrence_meta.create_all(engine)
        sessions = sessionmaker(bind=engine)
        local, remote = Source(), Source()
        server = SimpleNamespace(expected_hash=local.pin)
        peer = Peer(remote.pin, 'syn', 'https://peer')
        class Scheduler:
            schedulerKey = 'node-test'
            def every(self, key, name, args, interval):
                with sessions.begin() as session:
                    ensure_schedule(session, key, self.schedulerKey, name, args, interval)
        monkeypatch.setattr(runtime, 'WitnessStore', lambda *args: None)
        monkeypatch.setattr(runtime, 'CustodyAnchor', lambda *args: None)
        monkeypatch.setattr(runtime, 'reconcile', lambda *args: None)
        runtime.prepare(sessions, server, 'syn://thing/local', WitnessConfig(peers=(peer,)), Scheduler())
        with sessions() as session:
            assert session.execute(select(schedules.c.enabled)).scalar_one() is True
        runtime.prepare(sessions, server, 'syn://thing/local', WitnessConfig(), Scheduler())
        with sessions() as session:
            assert session.execute(select(schedules.c.enabled)).scalar_one() is False
        assert runtime.run_peer('unused', 'unused', 'unused', {}, peerHash=remote.pin,
            schedulerKey='old') == {'status': 'disabled', 'recorded': False}
    finally:
        engine.dispose()
