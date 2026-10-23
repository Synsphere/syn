"""Service/client contract, access isolation, retries and actual HTTP receipt verification."""

from __future__ import annotations

import socket
import time
from copy import deepcopy
from threading import Thread
from uuid import uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from syn_client import IntegrityError, SynClient, SynError
from syn_client.protocol import GENESIS_HASH, verify_receipt

from syn.communication import (
    MessageStore,
    communicationRouter,
    metadata,
    validateTokens,
)


@pytest.fixture
def rig():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata.create_all(engine)
    sessions = sessionmaker(engine)
    workspace = str(uuid4())
    token = "synthetic-workspace-token-" + "x" * 40
    app = FastAPI()
    app.include_router(communicationRouter(sessions, {workspace: token}))
    http = TestClient(app)
    http.headers["Authorization"] = "Bearer " + token
    yield app, http, workspace, token, MessageStore(sessions)
    http.close()
    engine.dispose()


def message(**changes):
    return {
        "message_id": str(uuid4()),
        "channel": "potbot.execution-events",
        "sender": "potbot",
        "payload": {"value": 1},
        **changes,
    }


def test_durable_receipts_replay_idempotence_and_conflicts(rig):
    _app, http, workspace, _token, store = rig
    url = f"/v1/communication/workspaces/{workspace}"
    first = message()
    one = http.post(url + "/messages", json=first)
    assert one.status_code == 201
    receipt = one.json()
    verify_receipt(receipt, workspace)
    assert receipt["previous_hash"] == GENESIS_HASH and receipt["sequence"] == 1
    assert http.post(url + "/messages", json=first).json() == receipt
    assert (
        http.post(
            url + "/messages", json={**first, "payload": {"value": 2}}
        ).status_code
        == 409
    )
    second = http.post(url + "/messages", json=message()).json()
    assert second["sequence"] == 2 and second["previous_hash"] == receipt["hash"]
    page = http.get(url + "/messages?limit=1").json()
    assert page == {"items": [receipt], "next_cursor": 1, "has_more": True}
    assert http.get(url + "/messages?after=1").json()["items"] == [second]
    assert store.head(workspace)["hash"] == second["hash"]
    assert http.get(url + "/head").json()["sequence"] == 2


def test_workspace_authentication_and_query_bounds(rig):
    _app, http, workspace, _token, _store = rig
    url = f"/v1/communication/workspaces/{workspace}/messages"
    assert http.get(url, headers={"Authorization": ""}).status_code == 403
    assert http.get(url.replace(workspace, str(uuid4()))).status_code == 403
    assert (
        http.post(url.replace(workspace, str(uuid4())), json=message()).status_code
        == 403
    )
    for query in (
        "?limit=0",
        "?limit=101",
        "?after=-1",
        "?limit=1&limit=2",
        "?unknown=1",
    ):
        assert http.get(url + query).status_code == 422


@pytest.mark.parametrize(
    "change",
    [
        {"payload": {"number": float("nan")}},
        {"payload": {"value": "a" * 65537}},
        {"payload": []},
        {"channel": "../wrong"},
        {"sender": "private\nheader"},
        {"message_id": "not-a-uuid"},
        {"extra": "not-allowed"},
    ],
)
def test_invalid_messages_never_append(rig, change):
    _app, http, workspace, _token, store = rig
    import json

    body = json.dumps(message(**change)).encode()
    response = http.post(
        f"/v1/communication/workspaces/{workspace}/messages", content=body
    )
    assert response.status_code == 422
    assert store.head(workspace)["sequence"] == 0


def test_body_bound_and_duplicate_json_fields(rig):
    _app, http, workspace, _token, _store = rig
    url = f"/v1/communication/workspaces/{workspace}/messages"
    assert http.post(url, content=b" " * 131073).status_code == 413
    assert http.post(url, content=b'{"payload":{},"payload":{}}').status_code == 422


def test_idempotency_compares_exact_json_types(rig):
    _app, http, workspace, _token, _store = rig
    url = f"/v1/communication/workspaces/{workspace}/messages"
    body = message(payload={"value": True})
    assert http.post(url, json=body).status_code == 201
    assert http.post(url, json={**body, "payload": {"value": 1}}).status_code == 409


def test_changed_receipts_and_wrong_workspace_are_rejected(rig):
    _app, _http, workspace, _token, store = rig
    receipt = store.append(workspace, message())
    bad = deepcopy(receipt)
    bad["payload"]["value"] = 9
    with pytest.raises(IntegrityError):
        verify_receipt(bad, workspace)
    with pytest.raises(IntegrityError):
        verify_receipt(receipt, str(uuid4()))


def test_real_http_client_restart_replay_and_retained_anchor(rig):
    app, _http, workspace, token, _store = rig
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        client = SynClient(url, token)
        body = message()
        receipt = client.send(workspace, **body)
        target = client.head(workspace)
        assert client.verify(workspace, expected_head=target) == target
        restarted = SynClient(url, token)
        assert restarted.send(workspace, **body) == receipt
        assert restarted.messages(workspace)["items"] == [receipt]
        with pytest.raises(SynError):
            SynClient(url, "wrong-token-" + "x" * 40).messages(workspace)
        with pytest.raises(IntegrityError):
            restarted.verify(workspace, expected_head={**target, "hash": GENESIS_HASH})
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
    assert not thread.is_alive()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
    ],
)
def test_client_rejects_insecure_destinations(url):
    with pytest.raises(ValueError):
        SynClient(url, "x" * 40)


def test_workspace_tokens_must_be_strong_and_unique():
    with pytest.raises(ValueError):
        validateTokens({str(uuid4()): "short"})
    with pytest.raises(ValueError):
        validateTokens({str(uuid4()): "x" * 40, str(uuid4()): "x" * 40})
