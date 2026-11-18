"""Public URL identity, strict wire boundary and dependency-free HTTP client."""
from __future__ import annotations

import hashlib
import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from syn_client.protocol import SynError
from syn_client.public_pubsub import (
    GROUP_PROFILE, MESSAGE_PROFILE, MAX_HTTP_RESPONSE_BYTES, MAX_MESSAGE_BYTES,
    PublicMessageTooLarge, PublicPubsubClient, PublicPubsubError, canonical_url,
    decode_message, encode_message, group_descriptor, group_hash, provider_key, topic,
)


URL = "syn://type/example"
MESSAGE_ID = "ab" * 16


def packet(**changes):
    return {"profile": MESSAGE_PROFILE, "network_id": "syn", "url": URL,
            "message_id": MESSAGE_ID, "sent_at": 1000, "data": {"value": 42}, **changes}


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


@pytest.mark.parametrize("kind", ["type", "thing", "event", "ember", "cog", "credential",
                                  "enigma", "pondle", "snug", "om"])
def test_every_existing_object_kind_and_alias_has_one_group(kind):
    canonical = f"syn://{kind}/object-1_name"
    alias = f"synny://{kind}/object-1_name"
    assert canonical_url(canonical) == canonical_url(alias) == canonical
    assert group_descriptor("syn", canonical) == group_descriptor("syn", alias)


def test_group_identity_has_explicit_stable_domain_and_binary_provider_key():
    body = b'{"network_id":"syn","profile":"syn.public-gossipsub-group/1","url":"syn://type/example"}'
    digest = hashlib.sha256(body).digest()
    descriptor = group_descriptor("syn", URL)
    assert descriptor == {"profile": GROUP_PROFILE, "network_id": "syn", "url": URL,
                          "group_hash": digest.hex(),
                          "topic": "/syn/public-pubsub/1/" + digest.hex(),
                          "provider_key": "1220" + digest.hex()}
    assert group_hash("syn", URL) == digest.hex()
    assert provider_key("syn", URL) == b"\x12\x20" + digest
    assert topic("syn", URL) == descriptor["topic"]
    assert group_hash("other-network", URL) != digest.hex()
    assert group_hash("syn", "syn://event/example") != digest.hex()
    assert group_hash("syn", "syn://type/different") != digest.hex()


@pytest.mark.parametrize("value", [None, 1, b"syn://type/example", "", "syn://", "https://type/example",
    "syn://unknown/example", "syn://Type/example", "syn://type/Example", "SYN://type/example",
    "syn://type/", "syn://type/a/b", "syn://type/a?query", "syn://type/a#fragment",
    "syn://type/a?", "syn://type/a#", "syn://user@type/a", "syn://type:123/a",
    "syn://type/%61", "syn://type/a.b", "syn://type/a--b", "syn://type/a__b",
    "syn://type/_a", "syn://type/a-", "syn://type/é", "syn://type/a\n", " syn://type/a",
    "syn://type/" + "a" * 256])
def test_public_groups_reject_noncanonical_or_non_syn_urls(value):
    with pytest.raises(PublicPubsubError):
        canonical_url(value)


def test_longest_slug_is_preserved_without_lossy_normalization():
    value = "synny://credential/" + "a" * 255
    assert canonical_url(value) == "syn://credential/" + "a" * 255


@pytest.mark.parametrize("network", [None, True, 3, "", "Syn", "a_b", "1syn", "syn/other", "a" * 65])
def test_invalid_network_is_rejected(network):
    with pytest.raises(PublicPubsubError):
        group_descriptor(network, URL)


@pytest.mark.parametrize("data", [None, True, False, 0, -17, 2 ** 100, 1.25, "hello\né🔥",
                                [1, "text", None], {"a": [True, {"b": 42}]}])
def test_message_round_trip_has_exact_canonical_fields(data):
    raw = encode_message("syn", "synny://type/example", data, message_id=MESSAGE_ID, sent_at=1000)
    assert raw == wire(packet(data=data))
    result = decode_message(raw, network_id="syn", url="synny://type/example", now=1000)
    assert result == packet(data=data)


def test_automatic_message_identity_is_fresh_and_timestamp_is_integer():
    first = decode_message(encode_message("syn", URL, None), network_id="syn", url=URL)
    second = decode_message(encode_message("syn", URL, None), network_id="syn", url=URL)
    assert first["message_id"] != second["message_id"]
    assert len(first["message_id"]) == 32
    assert type(first["sent_at"]) is int


@pytest.mark.parametrize("data", [float("nan"), float("inf"), -float("inf"), {1: "value"},
                                (1, 2), {"value": b"bytes"}, {"value": {1, 2}}])
def test_non_json_python_values_are_rejected(data):
    with pytest.raises(PublicPubsubError):
        encode_message("syn", URL, data)


@pytest.mark.parametrize("data", ["\ud800", "text\udfff", {"\ud800": "value"}, {"value": "\udc00"}])
def test_unpaired_surrogates_cannot_poison_wire_messages_or_http_pages(data):
    with pytest.raises(PublicPubsubError, match="Unicode"):
        encode_message("syn", URL, data)
    with pytest.raises(PublicPubsubError, match="Unicode"):
        decode_message(wire(packet(data=data)), network_id="syn", url=URL, now=1000)
    client = PublicPubsubClient("https://example.org")
    client._opener = Opener(response=Response(wire({"messages": [packet(data=data)]})))
    with pytest.raises(SynError):
        client.messages(group_hash("syn", URL))


def test_paired_surrogate_escapes_decode_to_valid_emoji():
    raw = wire(packet(data="😀"))
    assert b"\\ud83d\\ude00" in raw
    assert decode_message(raw, network_id="syn", url=URL, now=1000)["data"] == "😀"


def test_depth_boundary_and_cycles_are_rejected_before_serialization():
    data = None
    for _ in range(63):
        data = [data]
    raw = encode_message("syn", URL, data, message_id=MESSAGE_ID, sent_at=1000)
    assert decode_message(raw, network_id="syn", url=URL, now=1000)["data"] == data
    with pytest.raises(PublicPubsubError, match="nesting"):
        encode_message("syn", URL, [data])
    with pytest.raises(PublicPubsubError, match="nesting"):
        decode_message(wire(packet(data=[data])), network_id="syn", url=URL, now=1000)
    cycle = []
    cycle.append(cycle)
    with pytest.raises(PublicPubsubError):
        encode_message("syn", URL, cycle)


def test_size_limit_counts_exact_canonical_bytes_including_unicode_escapes():
    empty = encode_message("syn", URL, "", message_id=MESSAGE_ID, sent_at=1000)
    data = "a" * (MAX_MESSAGE_BYTES - len(empty))
    raw = encode_message("syn", URL, data, message_id=MESSAGE_ID, sent_at=1000)
    assert len(raw) == MAX_MESSAGE_BYTES
    assert decode_message(raw, network_id="syn", url=URL, now=1000)["data"] == data
    with pytest.raises(PublicMessageTooLarge):
        encode_message("syn", URL, data + "a", message_id=MESSAGE_ID, sent_at=1000)
    with pytest.raises(PublicMessageTooLarge):
        encode_message("syn", URL, "é" * 12000)
    with pytest.raises(PublicMessageTooLarge):
        decode_message(raw + b" ", network_id="syn", url=URL, now=1000)


@pytest.mark.parametrize("changes", [{"profile": "other"}, {"network_id": "other"},
    {"url": "syn://cog/example"}, {"url": "synny://type/example"}, {"extra": True},
    {"sent_at": 1000.0}, {"sent_at": True}, {"message_id": "AB" * 16},
    {"message_id": "00" * 15}, {"message_id": 3}, {"data": float("nan")}])
def test_wire_namespace_profile_fields_and_identity_are_exact(changes):
    with pytest.raises(PublicPubsubError):
        decode_message(wire(packet(**changes)), network_id="syn", url=URL, now=1000)


@pytest.mark.parametrize("raw", [b"", b"[]", b"null", b"not-json", b"\xff", b"{" * 1500,
                                 wire(packet()) + b"\n", b" " + wire(packet()),
                                 json.dumps(packet()).encode(),
                                 wire(packet()).replace(b'"data":', b'"data":null,"data":')])
def test_noncanonical_or_invalid_wire_json_is_rejected(raw):
    with pytest.raises(PublicPubsubError):
        decode_message(raw, network_id="syn", url=URL, now=1000)


@pytest.mark.parametrize("sent_at,valid", [(700, True), (699, False), (1000, True),
                                          (1030, True), (1031, False)])
def test_receive_freshness_boundaries(sent_at, valid):
    raw = wire(packet(sent_at=sent_at))
    if valid:
        assert decode_message(raw, network_id="syn", url=URL, now=1000)["sent_at"] == sent_at
    else:
        with pytest.raises(PublicPubsubError, match="stale or from the future"):
            decode_message(raw, network_id="syn", url=URL, now=1000)


@pytest.mark.parametrize("value", [-1, True, "1000", float("nan"), float("inf")])
def test_invalid_verification_clock_is_rejected(value):
    with pytest.raises(PublicPubsubError):
        decode_message(wire(packet()), network_id="syn", url=URL, now=value)


@pytest.mark.parametrize("changes", [{"message_id": "0"}, {"message_id": True},
                                    {"sent_at": -1}, {"sent_at": True}, {"sent_at": 1000.0}])
def test_encode_identity_fields_are_strict(changes):
    with pytest.raises(PublicPubsubError):
        encode_message("syn", URL, None, **changes)


class Response:
    def __init__(self, raw=b'{"ok":true}'):
        self.raw = raw
        self.requested_bytes = None

    def read(self, size):
        self.requested_bytes = size
        return self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Opener:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response or Response()
        self.error = error

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


def test_client_operations_use_normalized_urls_opaque_cursors_and_exact_methods():
    client = PublicPubsubClient("http://127.0.0.1:8000/", timeout=2)
    client._opener = opener = Opener()
    identity = "a" * 64
    assert client.groups() == {"ok": True}
    client.join("synny://cog/test")
    client.join("synny://cog/test", "syn-test")
    client.leave(identity)
    client.publish(identity, {"value": [1, "hello"]})
    client.messages(identity, after="opaque/+?=", limit=256)
    requests = [request for request, timeout in opener.calls if timeout == 2]
    assert [request.get_method() for request in requests] == ["GET", "POST", "POST", "DELETE", "POST", "GET"]
    prefix = "http://127.0.0.1:8000/p2p/pubsub/groups"
    assert requests[0].full_url == prefix
    assert json.loads(requests[1].data) == {"url": "syn://cog/test"}
    assert json.loads(requests[2].data) == {"url": "syn://cog/test", "network_id": "syn-test"}
    assert requests[3].full_url == prefix + "/" + identity
    assert requests[4].full_url == prefix + "/" + identity + "/messages"
    assert json.loads(requests[4].data) == {"data": {"value": [1, "hello"]}}
    assert parse_qs(urlsplit(requests[5].full_url).query) == {"after": ["opaque/+?="], "limit": ["256"]}
    assert all(request.get_header("Authorization") is None for request in requests)
    assert opener.response.requested_bytes == MAX_HTTP_RESPONSE_BYTES + 1


@pytest.mark.parametrize("url", ["http://example.org", "https://user@example.org", "https://@example.org",
    "https://example.org/path", "https://example.org?", "https://example.org#", "https://example.org/?x=1",
    "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:invalid",
    " https://example.org", "https://example.org\n", None])
def test_client_rejects_unsafe_or_ambiguous_service_urls(url):
    with pytest.raises(PublicPubsubError):
        PublicPubsubClient(url)


@pytest.mark.parametrize("url", ["https://example.org", "http://localhost:8000", "http://[::1]:8000"])
def test_client_accepts_https_and_loopback_service_urls(url):
    assert PublicPubsubClient(url).url == url


@pytest.mark.parametrize("timeout", [True, 0, -1, 121, 10 ** 400, "10", float("nan"), float("inf")])
def test_client_timeout_is_bounded(timeout):
    with pytest.raises(PublicPubsubError):
        PublicPubsubClient("https://example.org", timeout=timeout)


@pytest.mark.parametrize("operation", ["leave", "publish", "messages"])
@pytest.mark.parametrize("identity", ["../other", "A" * 64, "a" * 63, None])
def test_client_validates_group_hash_before_constructing_url(operation, identity):
    client = PublicPubsubClient("https://example.org")
    client._opener = opener = Opener()
    with pytest.raises(PublicPubsubError):
        getattr(client, operation)(identity, *([None] if operation == "publish" else []))
    assert not opener.calls


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 257}, {"limit": True},
                                    {"after": ""}, {"after": "x" * 513}, {"after": 1}])
def test_client_receive_parameters_are_bounded(kwargs):
    client = PublicPubsubClient("https://example.org")
    client._opener = opener = Opener()
    with pytest.raises(PublicPubsubError):
        client.messages("a" * 64, **kwargs)
    assert not opener.calls


@pytest.mark.parametrize("error", [URLError("private internal detail"), OSError("private internal detail"),
    HTTPError("https://example.org", 409, "private internal detail", {}, io.BytesIO(b"sensitive response")),
    HTTPError("https://example.org", 302, "redirect", {"Location": "https://elsewhere.invalid"}, io.BytesIO())])
def test_failed_publication_surfaces_safe_error_without_retry(error):
    client = PublicPubsubClient("https://example.org")
    client._opener = opener = Opener(error=error)
    with pytest.raises(SynError) as failure:
        client.publish("a" * 64, {"private": "not included in errors"})
    assert "private" not in str(failure.value)
    assert "sensitive" not in str(failure.value)
    assert len(opener.calls) == 1


@pytest.mark.parametrize("response", [b"not-json", b"[]", b'{"value":NaN}', b'{"value":1,"value":2}',
                                     b"x" * (MAX_HTTP_RESPONSE_BYTES + 1)])
def test_client_rejects_invalid_or_oversized_server_response(response):
    client = PublicPubsubClient("https://example.org")
    client._opener = Opener(response=Response(response))
    with pytest.raises(SynError):
        client.groups()


def test_client_receive_page_allows_maximum_wire_depth_plus_http_wrapping():
    data = None
    for _ in range(63):
        data = [data]
    message = decode_message(encode_message("syn", URL, data, message_id=MESSAGE_ID, sent_at=1000),
                             network_id="syn", url=URL, now=1000)
    cursor = "ab" * 16 + ":1"
    page = {"group_hash": group_hash("syn", URL), "stream_id": "ab" * 16,
            "messages": [{**message, "sender_peer_id": "transport-identity", "received_at": 1000,
                          "cursor": cursor}],
            "cursor": cursor, "dropped": 0, "has_more": False}
    client = PublicPubsubClient("https://example.org")
    client._opener = Opener(response=Response(json.dumps(page).encode()))
    assert client.messages(page["group_hash"]) == page
    # The extra page/list levels do not remove the nesting bound.
    page["messages"][0]["data"] = [data]
    client._opener = Opener(response=Response(json.dumps(page).encode()))
    with pytest.raises(SynError):
        client.messages(page["group_hash"])


def test_client_does_not_use_ambient_proxies_or_follow_redirects(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    client = PublicPubsubClient("https://example.org")
    redirect = next(handler for handler in client._opener.handlers
                    if hasattr(handler, "redirect_request"))
    assert redirect.redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.invalid") is None
    assert not any(hasattr(handler, "proxy_open") for handler in client._opener.handlers)
