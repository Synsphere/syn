from __future__ import annotations

import csv
import io
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from syn import (
    FEED_ONTOLOGY_VERSION,
    FEED_TYPE_BY_SLUG,
    FEED_TYPE_DEFINITIONS,
    MASTER_COLUMNS,
    MASTER_DATASET,
    buildFeedImportPlan,
    canonicalFeedUrl,
    classifyFeed,
    feedTypeSqlStatements,
    validateFeedOntology,
)
from syn.feeds import KNOWLEDGE_FIELDS


def feedRow(**changes: str) -> dict[str, str]:
    row = {
        "Corpus": "New corpus",
        "Category": "News and current affairs",
        "Subcategory": "Publisher syndication feed",
        "Publishing entity": "Example Publisher",
        "Source name": "Example Channel",
        "Data source URL": "https://example.com/feed.xml",
        "URL form": "Direct syndication endpoint",
        "Protocol / standard": "RSS/XML",
        "Data format / MIME": "application/rss+xml",
        "API query required": "No",
        "Authentication": "None",
        "Query parameters / example": "",
        "Geography": "Global",
        "Language": "English",
        "Discovery depth": "Official documentation",
        "Validation status": "Active",
        "Last checked / evidence date": "2026-07-29",
        "Evidence / documentation URL": "https://example.com/docs",
        "Notes": "Published hourly",
    }
    row.update(changes)
    return row


def writeBundle(path: Path, rows: list[dict[str, str]], *, columns=MASTER_COLUMNS) -> Path:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    with ZipFile(path, "w", ZIP_DEFLATED) as bundle:
        bundle.writestr(MASTER_DATASET, output.getvalue().encode("utf-8"))
        bundle.writestr("feed_mime_types_117.csv", b"Group,MIME type / Content-Type\n")
    return path


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("HTTPS://WWW.Example.COM/", "https://example.com"),
        ("https://example.com:443/feed#section", "https://example.com/feed"),
        ("http://www.example.com:80/feed?q=One", "http://example.com/feed?q=One"),
        ("https://example.com:8443/feed", "https://example.com:8443/feed"),
        ("https://example.com/Feed", "https://example.com/Feed"),
    ],
)
def test_canonical_feed_url_normalizes_only_safe_url_parts(source: str, expected: str) -> None:
    assert canonicalFeedUrl(source) == expected


def test_canonical_feed_url_preserves_http_and_https_as_distinct() -> None:
    assert canonicalFeedUrl("http://www.example.com/feed") != canonicalFeedUrl(
        "https://example.com/feed"
    )


@pytest.mark.parametrize(
    "value",
    ["", "example.com/feed", "ftp://example.com/feed", "https:///feed", "https://x:bad/feed"],
)
def test_canonical_feed_url_rejects_non_http_or_malformed_values(value: str) -> None:
    with pytest.raises(ValueError):
        canonicalFeedUrl(value)


def test_feed_ontology_is_versioned_polyhierarchical_and_reachable() -> None:
    validateFeedOntology()
    assert FEED_ONTOLOGY_VERSION == 1
    assert len(FEED_TYPE_DEFINITIONS) >= 75
    assert set(FEED_TYPE_BY_SLUG["podcast-feed"].parents) == {
        "rss-feed",
        "media-feed",
    }
    assert set(FEED_TYPE_BY_SLUG["sdmx-feed"].parents) == {
        "rest-api-feed",
        "statistical-data-feed",
    }


def test_feed_type_names_use_camel_case() -> None:
    assert FEED_TYPE_BY_SLUG["oai-pmh-feed"].name == "oaiPmhFeed"
    assert FEED_TYPE_BY_SLUG["traditional-asset-price-feed"].name == (
        "traditionalAssetPriceFeed"
    )


def test_feed_type_seed_sql_is_idempotent_and_complete() -> None:
    sql = "\n".join(feedTypeSqlStatements())
    assert "syn-feeds" in sql
    assert "feed_mime_types_117.csv" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert 'ON CONFLICT ("childRef", "parentRef") DO NOTHING' in sql
    for definition in FEED_TYPE_DEFINITIONS:
        assert definition.objectRef in sql


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"Protocol / standard": "RSS/XML"}, {"feed", "rss-feed", "news-feed"}),
        (
            {
                "Category": "Academic and research",
                "Subcategory": "Institutional repository",
                "Protocol / standard": "OAI-PMH",
                "Data format / MIME": "text/xml; application/xml",
            },
            {"academic-feed", "institutional-repository-feed", "oai-pmh-feed"},
        ),
        (
            {
                "Category": "Price and market data",
                "Subcategory": "Crypto prices",
                "Protocol / standard": "Crypto aggregator REST",
                "Data format / MIME": "application/json",
            },
            {"price-feed", "crypto-price-feed", "rest-api-feed", "json-data-feed"},
        ),
        (
            {
                "Category": "Government and official statistics",
                "Subcategory": "National statistical office / official statistics body",
                "Protocol / standard": "SDMX REST",
            },
            {"government-data-feed", "statistical-data-feed", "sdmx-feed"},
        ),
        (
            {
                "Category": "Books, publishing and libraries",
                "Protocol / standard": "OPDS/Atom",
            },
            {"book-data-feed", "opds-feed", "atom-feed"},
        ),
    ],
)
def test_feed_classifier_assigns_subject_delivery_and_representation_types(
    changes: dict[str, str], expected: set[str]
) -> None:
    assert expected <= set(classifyFeed(feedRow(**changes)))


def test_plan_preserves_every_source_field_and_adds_derived_knowledge(tmp_path: Path) -> None:
    bundle = writeBundle(tmp_path / "feeds.zip", [feedRow()])
    plan = buildFeedImportPlan(bundle)
    record = plan.records[0]
    assert plan.sourceCount == plan.uniqueCount == 1
    assert plan.duplicateCount == 0
    assert set(KNOWLEDGE_FIELDS.values()) <= record.knowledge.keys()
    assert record.knowledge["sourceDataset"] == MASTER_DATASET
    assert record.knowledge["canonicalUrl"] == "https://example.com/feed.xml"
    assert record.knowledge["requiresApiQuery"] is False
    assert record.knowledge["duplicateUrls"] == []
    assert record.knowledge["queryParametersExample"] is None
    assert record.knowledge["feedTypeRefs"]


def test_plan_generates_a_stable_local_thing_ref_from_the_url(tmp_path: Path) -> None:
    first = writeBundle(tmp_path / "first.zip", [feedRow(**{"Source name": "First"})])
    second = writeBundle(tmp_path / "second.zip", [feedRow(**{"Source name": "Renamed"})])
    firstRecord = buildFeedImportPlan(first).records[0]
    secondRecord = buildFeedImportPlan(second).records[0]
    assert firstRecord.objectRef == secondRecord.objectRef
    assert firstRecord.objectRef.startswith("syn://thing/feed-example-com-")
    assert len(firstRecord.objectRef.removeprefix("syn://thing/")) <= 255


def test_plan_merges_www_duplicate_urls_and_prefers_non_www(tmp_path: Path) -> None:
    rows = [
        feedRow(**{"Data source URL": "https://www.example.com/feed.xml"}),
        feedRow(
            **{
                "Data source URL": "https://example.com/feed.xml",
                "Notes": "Canonical host",
            }
        ),
    ]
    plan = buildFeedImportPlan(writeBundle(tmp_path / "feeds.zip", rows))
    assert plan.sourceCount == 2
    assert plan.uniqueCount == 1
    assert plan.duplicateCount == 1
    record = plan.records[0]
    assert record.knowledge["feedUrl"] == "https://example.com/feed.xml"
    assert record.knowledge["duplicateUrls"] == [
        "https://www.example.com/feed.xml",
        "https://example.com/feed.xml",
    ]
    assert record.knowledge["alternateValues"] == {
        "notes": ["Published hourly", "Canonical host"]
    }
    assert record.knowledge["sourceRowNumbers"] == [2, 3]


def test_plan_does_not_merge_http_and_https_rows(tmp_path: Path) -> None:
    rows = [
        feedRow(**{"Data source URL": "http://example.com/feed.xml"}),
        feedRow(**{"Data source URL": "https://example.com/feed.xml"}),
    ]
    plan = buildFeedImportPlan(writeBundle(tmp_path / "feeds.zip", rows))
    assert plan.uniqueCount == 2
    assert plan.duplicateCount == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Yes, symbols required", True), ("No / dataset-dependent", False), ("Optional", None)],
)
def test_plan_derives_query_requirement_without_losing_original_value(
    value: str, expected: bool | None, tmp_path: Path
) -> None:
    bundle = writeBundle(tmp_path / "feeds.zip", [feedRow(**{"API query required": value})])
    knowledge = buildFeedImportPlan(bundle).records[0].knowledge
    assert knowledge["apiQueryRequired"] == value
    assert knowledge["requiresApiQuery"] is expected


def test_description_summarizes_source_scope_delivery_and_status(tmp_path: Path) -> None:
    record = buildFeedImportPlan(
        writeBundle(tmp_path / "feeds.zip", [feedRow()])
    ).records[0]
    description = str(record.knowledge["description"])
    assert description.startswith("Example Channel is a publisher syndication feed")
    assert "published by Example Publisher" in description
    assert "Global" in description
    assert "English" in description
    assert "RSS/XML" in description
    assert "Active" in description
    assert description.endswith(".")


def test_plan_rejects_missing_master_dataset(tmp_path: Path) -> None:
    bundlePath = tmp_path / "missing.zip"
    with ZipFile(bundlePath, "w") as bundle:
        bundle.writestr("other.csv", b"x\n")
    with pytest.raises(ValueError, match=MASTER_DATASET):
        buildFeedImportPlan(bundlePath)


def test_plan_rejects_changed_master_columns(tmp_path: Path) -> None:
    bundle = writeBundle(tmp_path / "feeds.zip", [{"URL": "https://example.com"}], columns=("URL",))
    with pytest.raises(ValueError, match="Unexpected"):
        buildFeedImportPlan(bundle)


def test_plan_reports_row_number_for_missing_url(tmp_path: Path) -> None:
    bundle = writeBundle(tmp_path / "feeds.zip", [feedRow(**{"Data source URL": ""})])
    with pytest.raises(ValueError, match="row 2"):
        buildFeedImportPlan(bundle)
