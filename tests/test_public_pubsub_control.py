"""Public group configuration and bounded local HTTP controls."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import pytest

from syn.api import createApp
from syn.config import P2PConfig, loadConfig
from syn.p2p import Libp2pNode
from syn.public_pubsub import (
    PublicCursorExpired, PublicGroupNotJoined, PublicPubsubBusy, PublicPubsubUnavailable,
)
from syn.public_pubsub_http import MAX_CONTROL_BODY_BYTES
from syn_client.public_pubsub import PublicMessageTooLarge, group_descriptor


GROUP = group_descriptor("syn", "syn://type/public-example")
CURSOR = "a" * 32 + ":3"


def http_client(config=None, operator=None):
    app = createApp(Mock(), {}, p2pConfig=config or P2PConfig(enabled=False),
                    operatorRuntime=operator, communicationTokens={})
    return TestClient(app)


def test_public_pubsub_defaults_and_config_file(tmp_path):
    defaults = P2PConfig()
    assert defaults.publicPubsubEnabled
    assert defaults.publicPubsubMaxGroups == 32
    assert defaults.publicPubsubQueueSize == 256
    assert defaults.publicPubsubDiscoveryIntervalSeconds == 15
    assert defaults.publicPubsubProviderRepublishSeconds == 10800
    path = tmp_path / "config.toml"
    path.write_text('''[database]
url="sqlite://"
[p2p]
publicPubsubEnabled=false
publicPubsubMaxGroups=4
publicPubsubQueueSize=8
publicPubsubDiscoveryIntervalSeconds=2.5
publicPubsubProviderRepublishSeconds=120
''')
    actual = loadConfig(path).p2p
    assert not actual.publicPubsubEnabled
    assert actual.publicPubsubMaxGroups == 4
    assert actual.publicPubsubQueueSize == 8
    assert actual.publicPubsubDiscoveryIntervalSeconds == 2.5
    assert actual.publicPubsubProviderRepublishSeconds == 120


@pytest.mark.parametrize("key,value", [
    ("publicPubsubEnabled", 1), ("publicPubsubMaxGroups", True),
    ("publicPubsubMaxGroups", 0), ("publicPubsubMaxGroups", 129),
    ("publicPubsubQueueSize", 1.0), ("publicPubsubQueueSize", 0),
    ("publicPubsubQueueSize", 4097), ("publicPubsubDiscoveryIntervalSeconds", True),
    ("publicPubsubDiscoveryIntervalSeconds", 0.5),
    ("publicPubsubDiscoveryIntervalSeconds", float("nan")),
    ("publicPubsubDiscoveryIntervalSeconds", 301),
    ("publicPubsubProviderRepublishSeconds", 59),
    ("publicPubsubProviderRepublishSeconds", 79201),
    ("publicPubsubProviderRepublishSeconds", float("inf")),
])
def test_public_pubsub_bounds_are_enforced(key, value):
    with pytest.raises(ValueError, match=key):
        P2PConfig(**{key: value})


def test_disabled_public_transport_reports_status_and_rejects_control():
    http = http_client()
    result = http.get("/p2p/pubsub/groups")
    assert result.status_code == 200
    assert result.json() == {"enabled": False, "running": False,
                             "default_network_id": "syn", "groups": []}
    assert http.get("/p2p/status").json()["publicPubsub"] == result.json()
    assert http.post("/p2p/pubsub/groups", json={"url": GROUP["url"]}).status_code == 503
    assert http.delete("/p2p/pubsub/groups/" + GROUP["group_hash"]).status_code == 503


def test_operator_network_is_default_without_a_public_membership_allowlist():
    node = Libp2pNode(P2PConfig(enabled=False),
                      SimpleNamespace(config={"network_id": "another-network"}))
    node._call_public = AsyncMock(return_value=GROUP)
    asyncio.run(node.join_public_group("synny://cog/open-example"))
    node._call_public.assert_awaited_once_with("join", "syn://cog/open-example", "another-network")
    node._call_public.reset_mock()
    asyncio.run(node.join_public_group("syn://ember/open-example", "syn"))
    node._call_public.assert_awaited_once_with("join", "syn://ember/open-example", "syn")


def test_http_controls_preserve_public_data_and_independent_read_cursor():
    http = http_client()
    node = http.app.state.p2pNode
    node.join_public_group = AsyncMock(return_value=GROUP)
    node.publish_public = AsyncMock(return_value={"accepted": True, "peer_reception_proved": False})
    node.read_public = AsyncMock(return_value={"messages": [], "cursor": CURSOR, "dropped": 0})
    node.leave_public_group = AsyncMock(return_value={"group_hash": GROUP["group_hash"], "joined": False})
    result = http.post("/p2p/pubsub/groups", json={"url": "synny://type/public-example", "network_id": "syn"})
    assert result.status_code == 200 and result.json() == GROUP
    node.join_public_group.assert_awaited_once_with("synny://type/public-example", "syn")
    messages = "/p2p/pubsub/groups/" + GROUP["group_hash"] + "/messages"
    payload = {"event": "observed", "value": [1, True, None, "public"]}
    assert http.post(messages, json={"data": payload}).json()["accepted"] is True
    node.publish_public.assert_awaited_once_with(GROUP["group_hash"], payload)
    assert http.get(messages, params={"after": CURSOR, "limit": 20}).json()["cursor"] == CURSOR
    node.read_public.assert_awaited_once_with(GROUP["group_hash"], CURSOR, 20)
    assert http.delete("/p2p/pubsub/groups/" + GROUP["group_hash"]).json()["joined"] is False
    node.leave_public_group.assert_awaited_once_with(GROUP["group_hash"])


@pytest.mark.parametrize("body", [
    {"url": "https://example.com/type/foo"}, {"url": "syn://type/../event"},
    {"url": "syn://type/foo", "network_id": "Syn"},
    {"url": "syn://type/foo", "network_id": 1}, {"url": 1},
    {"url": "syn://type/foo", "members": []},
])
def test_join_input_is_rejected_before_transport_access(body):
    http = http_client()
    node = http.app.state.p2pNode
    node._call_public = AsyncMock(side_effect=AssertionError("Invalid group reached transport"))
    assert http.post("/p2p/pubsub/groups", json=body).status_code == 422
    node._call_public.assert_not_called()


@pytest.mark.parametrize("suffix", [
    "?limit=0", "?limit=257", "?after=bad", "?after=" + "a" * 32 + ":-1",
    "?after=" + "a" * 32 + ":01",
])
def test_read_input_is_bounded_before_transport_access(suffix):
    http = http_client()
    node = http.app.state.p2pNode
    node.read_public = AsyncMock(side_effect=AssertionError("Invalid cursor reached transport"))
    assert http.get("/p2p/pubsub/groups/" + GROUP["group_hash"] + "/messages" + suffix).status_code == 422
    node.read_public.assert_not_called()


@pytest.mark.parametrize("error,code", [
    (PublicMessageTooLarge("internal data"), 413),
    (PublicCursorExpired("internal data"), 409),
    (PublicGroupNotJoined("internal data"), 404),
    (PublicPubsubBusy("internal data"), 429),
    (PublicPubsubUnavailable("internal data"), 503),
    (ValueError("internal data"), 422),
])
def test_public_api_errors_are_distinct_and_do_not_expose_internal_details(error, code):
    http = http_client()
    http.app.state.p2pNode.publish_public = AsyncMock(side_effect=error)
    response = http.post("/p2p/pubsub/groups/" + GROUP["group_hash"] + "/messages", json={"data": None})
    assert response.status_code == code
    assert "internal" not in response.text


@pytest.mark.parametrize("raw", [
    b'{"data":NaN}', b'{"data":Infinity}', b'{"data":1e9999}',
    b'{"data":1,"data":2}', b'{"data":{"x":1,"x":2}}',
    b'{"data":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}',
    b'{"data":"\\ud800"}', b'{"data":{"\\udfff":1}}',
], ids=["nan", "infinity", "overflow", "duplicate-field", "duplicate-nested", "deep",
        "surrogate-value", "surrogate-key"])
def test_invalid_json_is_rejected_without_starting_publication(raw):
    http = http_client()
    node = http.app.state.p2pNode
    node.publish_public = AsyncMock(side_effect=AssertionError("Invalid JSON reached publication"))
    response = http.post("/p2p/pubsub/groups/" + GROUP["group_hash"] + "/messages",
                         content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    node.publish_public.assert_not_called()


@pytest.mark.parametrize("chunked", [False, True])
def test_control_body_bound_applies_before_json_parsing_even_without_content_length(chunked):
    http = http_client()
    node = http.app.state.p2pNode
    node.publish_public = AsyncMock(side_effect=AssertionError("Oversized body reached publication"))
    raw = json.dumps({"data": "x" * MAX_CONTROL_BODY_BYTES}).encode()
    content = (chunk for chunk in (raw[:1024], raw[1024:])) if chunked else raw
    response = http.post("/p2p/pubsub/groups/" + GROUP["group_hash"] + "/messages",
                         content=content, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    node.publish_public.assert_not_called()


def test_readiness_fence_covers_public_controls():
    http = http_client()
    http.app.state.awarenessFault = RuntimeError("transport stopped")
    assert http.get("/p2p/pubsub/groups").status_code == 503
    assert http.post("/p2p/pubsub/groups", json={"url": GROUP["url"]}).status_code == 503


def test_public_group_operations_are_documented_without_private_scope_requirements():
    schema = http_client().get("/openapi.json").json()
    assert {"get", "post"} <= schema["paths"]["/p2p/pubsub/groups"].keys()
    assert "delete" in schema["paths"]["/p2p/pubsub/groups/{group_hash}"]
    assert {"get", "post"} <= schema["paths"]["/p2p/pubsub/groups/{group_hash}/messages"].keys()
    join = schema["components"]["schemas"]["JoinPublicGroup"]
    assert join["required"] == ["url"]
    assert set(join["properties"]) == {"url", "network_id"}
