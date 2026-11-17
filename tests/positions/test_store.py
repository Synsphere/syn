from dataclasses import replace
from datetime import timedelta
import pytest
from sqlalchemy import select, func
from syn.positions.types import geographic, Reading, Space, Estimate
from syn.positions import schema as db
from syn.positions.store import workspace_for

NODE="syn://thing/test-node"


def prior(now):
    return geographic(51,-.1,None,100000,now,"timezone")
def reading(now,lat=51.5,error=3):
    return Reading("gps",now,now,geographic(lat,-.1,20,error,now,"gps",altitudeDatum="ellipsoid"),"3d", "Test receiver", "123",8)


def test_bootstrap_idempotent_and_ledger(state,now):
    store,sessions,ledger=state
    first=store.bootstrap(NODE,prior(now))
    again=store.bootstrap(NODE,prior(now+timedelta(days=1)))
    assert first == again
    verified=store.verify(NODE)
    assert verified["objects"] == 1 and verified["sequence"] == 2


def test_reading_event_ember_canonical_atomic(state,now):
    store,sessions,ledger=state
    store.bootstrap(NODE,prior(now))
    result=store.observe(NODE,reading(now))
    assert result["decision"] == "replace"
    assert result["eventRef"].startswith("syn://event/")
    assert result["emberRef"].startswith("syn://ember/")
    assert store.current(NODE)[0]["revision"] == 2
    verified=store.verify(NODE)
    assert verified["objects"] == 3 and verified["sequence"] == 5
    assert store.observe(NODE,reading(now))["decision"] == "duplicate"
    assert store.verify(NODE) == verified


def test_stationary_confirmation_does_not_make_ember(state,now):
    store,sessions,ledger=state
    store.bootstrap(NODE,prior(now))
    store.observe(NODE,reading(now))
    later=now+timedelta(seconds=1)
    result=store.observe(NODE,reading(later))
    assert result["decision"] == "unchanged"
    assert store.current(NODE)[0]["revision"] == 2
    assert store.verify(NODE)["objects"] == 4


def test_stale_and_nofix_keep_belief_but_record_events(state,now):
    store,sessions,ledger=state
    store.bootstrap(NODE,prior(now))
    store.observe(NODE,reading(now))
    before=store.current(NODE)
    nofix=Reading("gps",now+timedelta(seconds=1),now+timedelta(seconds=1),None,"none")
    assert store.observe(NODE,nofix)["decision"] == "no-fix"
    assert store.observe(NODE,reading(now-timedelta(minutes=10)),now=now)["decision"] == "stale"
    assert before == store.current(NODE)
    assert store.verify(NODE)["objects"] == 5


def test_arbitrary_space_independent(state,now):
    store,sessions,ledger=state
    store.bootstrap(NODE,prior(now))
    frame=Space("syn://thing/robot-frame","syn://type/robot-frame-position",("x","y","z"),("metre",)*3,"metre","euclidean")
    store.register_space(frame)
    e=Estimate(frame.reference,frame.typeRef,{"x":1,"y":2,"z":3},.1,"metre",now,"robot")
    store.put(NODE,e,now=now)
    assert len(store.current(NODE)) == 2
    assert store.verify(NODE)["spaces"] == 2
    with pytest.raises(ValueError):
        store.register_space(replace(frame,units=("foot",)*3))


def test_ledger_failure_rolls_back_everything(state,now,monkeypatch):
    store,sessions,ledger=state
    store.bootstrap(NODE,prior(now))
    before=store.current(NODE)
    head=store.ledger.head(workspace_for(NODE))
    real=store.ledger.appendInTransaction
    calls=0
    def fail(session,workspace,body):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected canonical-ledger failure")
        return real(session,workspace,body)
    monkeypatch.setattr(store.ledger,"appendInTransaction",fail)
    with pytest.raises(RuntimeError):
        store.observe(NODE,reading(now))
    assert store.current(NODE) == before
    assert store.ledger.head(workspace_for(NODE)) == head
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(db.primitive["events"])) == 0
        assert session.scalar(select(func.count()).select_from(db.records)) == 1


def test_tampering_detected_by_verifier(state,now):
    store,sessions,ledger=state
    store.bootstrap(NODE,prior(now))
    row=store.current(NODE)[0]
    with sessions.begin() as session:
        session.execute(db.primitive["embers"].update().where(db.primitive["embers"].c.id == row["emberRef"]).values(name="tampered"))
    with pytest.raises(ValueError,match="altered"):
        store.verify(NODE)


def test_canonical_pointer_and_head_tampering_detected(state,now):
    store,sessions,ledger=state
    store.bootstrap(NODE,prior(now))
    with sessions.begin() as session:
        session.execute(db.positions.update().where(db.positions.c.nodeRef == NODE).values(revision=44))
    with pytest.raises(ValueError,match="pointer"):
        store.verify(NODE)


def test_no_cross_node_or_space_collisions(state,now):
    store,sessions,_=state
    other="syn://thing/other-node"
    for node in (NODE,other):
        store.bootstrap(node,prior(now))
    store.observe(NODE,reading(now))
    assert store.current(other)[0]["revision"] == 1
    assert store.verify(other)["sequence"] == 2


def test_space_cannot_take_over_existing_thing(state,now):
    store,sessions,_=state
    with sessions.begin() as session:
        session.execute(db.primitive["things"].insert().values(id="syn://thing/existing-object",typeRef="syn://type/device",name="Unrelated",knowledge={}))
    frame=Space("syn://thing/existing-object","syn://type/new-space-position",("x",),("metre",),"metre")
    with pytest.raises(ValueError,match="unused Thing"):
        store.register_space(frame)


def test_event_timestamp_tampering_detected(state,now):
    store,sessions,_=state
    store.bootstrap(NODE,prior(now))
    event=store.observe(NODE,reading(now))["eventRef"]
    with sessions.begin() as session:
        session.execute(db.primitive["events"].update().where(db.primitive["events"].c.id==event).values(occurredAt=now+timedelta(seconds=1)))
    with pytest.raises(ValueError,match="timestamp"):
        store.verify(NODE)
