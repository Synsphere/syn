"""Scheduled proof-verified peer pull on Syn's existing ProcessScheduler."""
from __future__ import annotations

from functools import partial
import hashlib
import logging

from cryptography.exceptions import InvalidSignature
from sqlalchemy import select

from .anchors import CustodyAnchor, reconcile
from .config import WitnessConfig
from .locking import ownership
from .protocol import RejectedEvidence, scoped_checkpoint
from .store import WitnessStore
from .transport import PeerClient, PeerUnavailable

HANDLER = "operator-dag-witness"
log = logging.getLogger(__name__)


def schedule_key(observer, peer):
    return "dag-witness-" + hashlib.sha256((observer + ":" + peer).encode("ascii")).hexdigest()


def poll_peer(store, peer, anchor, client):
    if peer.operator == store.observer:
        raise ValueError("An Operator cannot be its own independent witness")
    # A retained custody checkpoint is checked BEFORE the untrusted endpoint is
    # contacted. Network data must never repair a missing/rolled-back archive.
    state = reconcile(store, peer, anchor)
    previous = state["checkpoint"]
    if state["conflicted"]:
        return {"status": "quarantined", "recorded": False}
    try:
        candidate = client.checkpoint()
        scoped_checkpoint(candidate, peer)
    except PeerUnavailable as error:
        return store.record(peer, previous=previous, failure=error.outcome)
    except OSError:
        return store.record(peer, previous=previous, failure="unavailable")
    except (ValueError, TypeError, KeyError, InvalidSignature):
        return store.record(peer, previous=previous, failure="invalid-checkpoint")
    proof = None
    if previous is not None and candidate["body"]["sequence"] > previous["body"]["sequence"]:
        try:
            proof = client.consistency(previous["body"]["sequence"], candidate["body"]["sequence"])
        except (PeerUnavailable, OSError):
            return store.record(peer, previous=previous, failure="unavailable")
        except (ValueError, TypeError, KeyError):
            return store.record(peer, previous=previous, failure="invalid-proof")
    try:
        result = store.record(peer, previous=previous, candidate=candidate, consistency=proof)
    except (RejectedEvidence, InvalidSignature):
        result = store.record(peer, previous=previous, failure="invalid-proof")
    # This call happens AFTER the transaction commits. A failed custody write
    # is a local failure, not a remote outage; propagate it so it is retried.
    reconcile(store, peer, anchor)
    return result


def run_peer(databaseUrl, configPath, nodeRef, settings, *, peerHash, schedulerKey):
    from pathlib import Path
    from syn.database import createEngine, createSessionFactory
    from syn.operator.service import OperatorServer
    policy = WitnessConfig.from_dict(settings)
    peer = next((p for p in policy.peers if p.operator == peerHash), None)
    if peer is None:
        return {"status": "disabled", "recorded": False}
    engine = createEngine(databaseUrl, migrate=False)
    try:
        sessions = createSessionFactory(engine)
        server = OperatorServer(Path(configPath))
        from syn.positions.types import digest
        if schedulerKey != "node-" + digest(nodeRef):
            raise ValueError("Witness job belongs to another node scheduler")
        store = WitnessStore(sessions, server.expected_hash, nodeRef)
        with ownership(sessions, "syn.witness:" + server.expected_hash + ":" + peer.operator):
            result = poll_peer(store, peer, CustodyAnchor(server, peer), PeerClient(peer, policy.timeoutSeconds))
        if result["status"] in {"equivocation", "checkpoint-conflict", "quarantined"}:
            log.error("Witness peer %s is quarantined: %s", peer.operator, result["status"])
        return result
    finally:
        engine.dispose()


def handler(databaseUrl, configPath, nodeRef, config):
    return partial(run_peer, databaseUrl, str(configPath), nodeRef, config.to_dict())


def prepare(sessions, server, nodeRef, config, scheduler):
    """Recover independent anchors and reconcile recurring jobs, including removals."""
    from syn.recurrence import schedules
    desired = set()
    for peer in config.peers:
        if peer.operator == server.expected_hash:
            raise ValueError("Witness peer must be an independent Operator")
        store = WitnessStore(sessions, server.expected_hash, nodeRef)
        with ownership(sessions, "syn.witness:" + server.expected_hash + ":" + peer.operator):
            reconcile(store, peer, CustodyAnchor(server, peer))
        key = schedule_key(server.expected_hash, peer.operator)
        desired.add(key)
        scheduler.every(key, HANDLER, {"peerHash": peer.operator, "schedulerKey": scheduler.schedulerKey},
                        config.pollSeconds)
    with sessions.begin() as session:
        existing = session.execute(select(schedules.c.scheduleKey).where(
            schedules.c.schedulerKey == scheduler.schedulerKey, schedules.c.handler == HANDLER)).scalars().all()
        for key in existing:
            if key not in desired:
                session.execute(schedules.update().where(schedules.c.scheduleKey == key,
                    schedules.c.schedulerKey == scheduler.schedulerKey).values(enabled=False))


def check_local_jobs(sessions, schedulerKey):
    """Remote outages return observations; failed local continuity jobs fence readiness."""
    from syn.recurrence import schedules
    from syn.models import Job, JobStatus
    from sqlalchemy import or_
    with sessions() as session:
        failed = session.execute(select(schedules.c.scheduleKey).outerjoin(
            Job, schedules.c.currentJobId == Job.id).where(
                schedules.c.schedulerKey == schedulerKey, schedules.c.enabled.is_(True),
                schedules.c.handler.in_([HANDLER, "operator-dag-sync"]),
                or_(schedules.c.failures > 0, Job.status == JobStatus.failed)).limit(1)).first()
    if failed is not None:
        raise RuntimeError("Independent DAG custody/witness verification lost readiness")
