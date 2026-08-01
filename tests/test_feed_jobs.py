from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from syn.feed_jobs import (
    FEED_WITNESS_HANDLER,
    FeedCandidate,
    HttpWitness,
    defaultJobHandlers,
    evaluateStaleFeeds,
    feedActivityScore,
    parseFeedEntryTimestamps,
    _buildWitnessObjects,
)
from syn import Event, EventTarget, ObjectRelationship, ObjectType, Thing


def test_activity_score_combines_weekly_updates_and_request_lag() -> None:
    requested = datetime(2026, 7, 30, tzinfo=UTC)
    newest = requested - timedelta(hours=12)
    timestamps = (
        newest - timedelta(days=8),
        newest - timedelta(days=3),
        newest,
    )
    assert feedActivityScore(requested, timestamps) == pytest.approx(2 / 12)
    assert feedActivityScore(None, timestamps) == 0
    assert feedActivityScore(requested, ()) == 0


def test_feed_timestamp_parser_supports_rss_atom_and_json_feed() -> None:
    xml = b"""<rss><channel><item><pubDate>Wed, 29 Jul 2026 12:00:00 GMT</pubDate></item>
        <item><updated>2026-07-30T08:30:00Z</updated></item></channel></rss>"""
    jsonBody = b'{"items":[{"date_published":"2026-07-28T10:00:00Z"}]}'
    assert [value.isoformat() for value in parseFeedEntryTimestamps(xml, "application/rss+xml")] == [
        "2026-07-29T12:00:00+00:00", "2026-07-30T08:30:00+00:00"
    ]
    assert parseFeedEntryTimestamps(jsonBody, "application/feed+json")[0].day == 28


def test_stale_feed_job_processes_highest_activity_until_target_remains(monkeypatch) -> None:
    now = datetime(2026, 7, 30, tzinfo=UTC)
    candidates = [
        FeedCandidate("syn://thing/slow", "https://slow.example/feed", now, (now - timedelta(days=4),)),
        FeedCandidate(
            "syn://thing/active", "https://active.example/feed", now,
            (now - timedelta(hours=2), now - timedelta(hours=1)),
        ),
        FeedCandidate("syn://thing/unknown", "https://unknown.example/feed", None, ()),
    ]
    selected = []
    monkeypatch.setattr("syn.feed_jobs._oldestFeedCandidates", lambda *_: candidates.copy())

    def fetch(url, *_, **__):
        selected.append(url)
        return HttpWitness(
            url, 200, "OK", "203.0.113.1", {}, {"content-type": ["application/json"]},
            b'{"date_published":"2026-07-30T00:00:00Z"}',
            {"startedAt": now.isoformat(), "finishedAt": now.isoformat(), "stages": []},
        )

    monkeypatch.setattr("syn.feed_jobs.timedHttpGet", fetch)
    monkeypatch.setattr("syn.feed_jobs._storeWitness", lambda *args: ("syn://event/witness", 1))
    result = evaluateStaleFeeds("unused", shortlistSize=3, remaining=1)
    assert selected == ["https://active.example/feed", "https://slow.example/feed"]
    assert result["remaining"] == 1
    assert len(result["requested"]) == 2
    assert any("Selected syn://thing/active" in entry for entry in result["logs"])


def test_feed_witness_handler_is_registered_by_default() -> None:
    handler = defaultJobHandlers("postgresql+psycopg://localhost/syn")[FEED_WITNESS_HANDLER]
    assert handler.func is evaluateStaleFeeds
    assert handler.args == ("postgresql+psycopg://localhost/syn",)


def test_failure_result_contains_type_repr_arguments_traceback_and_log(monkeypatch) -> None:
    candidate = FeedCandidate("syn://thing/broken", "https://broken.example/feed", None, ())
    monkeypatch.setattr("syn.feed_jobs._oldestFeedCandidates", lambda *_: [candidate, candidate])

    def fail(*args, **kwargs):
        error = RuntimeError()
        error.add_note("Failed action: TLS handshake")
        raise error

    monkeypatch.setattr("syn.feed_jobs.timedHttpGet", fail)
    result = evaluateStaleFeeds("unused", shortlistSize=2, remaining=1)
    details = result["failures"][0]["error"]
    assert details["type"] == "builtins.RuntimeError"
    assert details["message"] == ""
    assert details["repr"] == "RuntimeError()"
    assert details["arguments"] == []
    assert "raise error" in details["traceback"]
    assert details["notes"] == ["Failed action: TLS handshake"]
    assert any("builtins.RuntimeError: RuntimeError()" in entry for entry in result["logs"])


def test_dry_run_parses_response_without_storing_event(monkeypatch) -> None:
    now = datetime(2026, 7, 30, tzinfo=UTC)
    candidate = FeedCandidate("syn://thing/feed", "https://example.com/feed", None, ())
    monkeypatch.setattr("syn.feed_jobs._oldestFeedCandidates", lambda *_: [candidate, candidate])
    monkeypatch.setattr(
        "syn.feed_jobs.timedHttpGet",
        lambda *args, **kwargs: HttpWitness(
            candidate.url, 200, "OK", "93.184.216.34", {},
            {"content-type": ["application/feed+json"]},
            b'{"date_published":"2026-07-30T00:00:00Z"}',
            {"startedAt": now.isoformat(), "finishedAt": now.isoformat(), "stages": []},
        ),
    )
    store = Mock()
    monkeypatch.setattr("syn.feed_jobs._storeWitness", store)
    result = evaluateStaleFeeds("unused", shortlistSize=2, remaining=1, dryRun=True)
    store.assert_not_called()
    assert result["dryRun"] is True
    assert result["requested"][0]["eventRef"] is None
    assert result["requested"][0]["entryTimestampCount"] == 1


def test_witness_builds_observation_result_and_separate_network_stats_with_redaction() -> None:
    now = datetime(2026, 7, 30, tzinfo=UTC)
    candidate = FeedCandidate("syn://thing/feed", "https://example.com/feed", now, ())
    witness = HttpWitness(
        candidate.url, 200, "OK", "93.184.216.34",
        {"Authorization": "Bearer secret", "Accept": "application/json"},
        {"set-cookie": ["secret=1"], "content-type": ["application/feed+json"]},
        b'{"date_published":"2026-07-30T00:00:00Z"}',
        {
            "startedAt": now.isoformat(), "finishedAt": now.isoformat(),
            "requests": [{"url": candidate.url, "status": 200, "timings": {"stages": []}}],
        },
    )
    objects, eventRef, entryCount = _buildWitnessObjects(candidate, witness)
    event = next(item for item in objects if isinstance(item, Event))
    things = [item for item in objects if isinstance(item, Thing)]
    types = [item for item in objects if isinstance(item, ObjectType)]
    targets = [item for item in objects if isinstance(item, EventTarget)]
    relationships = [item for item in objects if isinstance(item, ObjectRelationship)]
    observation = next(item for item in things if item.name == "httpObservationResult")
    network = next(item for item in things if item.name == "networkStats")

    assert event.id == eventRef
    assert event.knowledge["observationRef"] == observation.id
    assert event.knowledge["networkStatsRef"] == network.id
    assert observation.knowledge["request"]["headersSent"]["Authorization"] == "[REDACTED]"
    assert observation.knowledge["request"]["headersReceived"]["set-cookie"] == "[REDACTED]"
    assert observation.knowledge["request"]["headersSent"]["Accept"] == "application/json"
    assert network.stats["serverIp"] == "93.184.216.34"
    assert network.stats["requestCount"] == 1
    assert entryCount == 1
    assert {item.typeRef for item in types} == {
        "syn://type/observation", "syn://type/observation-result",
        "syn://type/network-measurement"
    }
    assert {item.role for item in targets} == {
        "feed", "feed-url", "observation-result", "network-stats"
    }
    assert {item.relationshipType for item in relationships} == {
        "has-observation-result", "has-network-stats", "measured-with"
    }
