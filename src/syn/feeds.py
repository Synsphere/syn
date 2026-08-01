from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit
from zipfile import ZipFile

from sqlalchemy import Connection, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as postgresqlInsert

from .models import Thing
from .ontology import TYPE_DEFINITIONS, TypeDefinition, validateOntology
from .refs import ObjectRef, normalizeSlug


FEED_ONTOLOGY_VERSION = 1
MASTER_DATASET = "global_data_sources_master_2326.csv"

MASTER_COLUMNS: tuple[str, ...] = (
    "Corpus",
    "Category",
    "Subcategory",
    "Publishing entity",
    "Source name",
    "Data source URL",
    "URL form",
    "Protocol / standard",
    "Data format / MIME",
    "API query required",
    "Authentication",
    "Query parameters / example",
    "Geography",
    "Language",
    "Discovery depth",
    "Validation status",
    "Last checked / evidence date",
    "Evidence / documentation URL",
    "Notes",
)

KNOWLEDGE_FIELDS: Mapping[str, str] = {
    "Corpus": "corpus",
    "Category": "category",
    "Subcategory": "subcategory",
    "Publishing entity": "publishingEntity",
    "Source name": "sourceName",
    "Data source URL": "feedUrl",
    "URL form": "urlForm",
    "Protocol / standard": "protocolStandard",
    "Data format / MIME": "dataFormatMime",
    "API query required": "apiQueryRequired",
    "Authentication": "authentication",
    "Query parameters / example": "queryParametersExample",
    "Geography": "geography",
    "Language": "language",
    "Discovery depth": "discoveryDepth",
    "Validation status": "validationStatus",
    "Last checked / evidence date": "lastCheckedEvidenceDate",
    "Evidence / documentation URL": "evidenceDocumentationUrl",
    "Notes": "notes",
}


def _feedType(
    slug: str,
    *parents: str,
    module: str,
    definition: str,
) -> TypeDefinition:
    return TypeDefinition(slug, tuple(parents), module, definition)


FEED_TYPE_DEFINITIONS: tuple[TypeDefinition, ...] = (
    _feedType(
        "feed",
        "information-object",
        module="feed",
        definition="A retrievable source that publishes or exposes a changing collection of data.",
    ),
    _feedType("syndication-feed", "feed", module="delivery", definition="A feed published using a web-syndication format."),
    _feedType("rss-feed", "syndication-feed", "xml-data-feed", module="delivery", definition="A web-syndication feed represented using RSS."),
    _feedType("atom-feed", "syndication-feed", "xml-data-feed", module="delivery", definition="A web-syndication feed represented using Atom."),
    _feedType("json-feed", "syndication-feed", "json-data-feed", module="delivery", definition="A web-syndication feed represented using JSON Feed."),
    _feedType("activity-stream-feed", "syndication-feed", "json-data-feed", module="delivery", definition="A feed represented using ActivityStreams or ActivityPub conventions."),
    _feedType("calendar-feed", "syndication-feed", module="delivery", definition="A feed of calendar events or schedules."),
    _feedType("stream-feed", "feed", module="delivery", definition="A continuously or incrementally delivered data feed."),
    _feedType("server-sent-events-feed", "stream-feed", module="delivery", definition="A stream delivered using Server-Sent Events."),
    _feedType("websocket-feed", "stream-feed", module="delivery", definition="A stream delivered using WebSocket."),
    _feedType("webhook-feed", "stream-feed", module="delivery", definition="A source that pushes updates to a registered webhook endpoint."),
    _feedType("api-feed", "feed", module="delivery", definition="A feed exposed through an application programming interface."),
    _feedType("rest-api-feed", "api-feed", module="delivery", definition="An API feed exposed using REST-style HTTP operations."),
    _feedType("graphql-feed", "api-feed", module="delivery", definition="An API feed queried using GraphQL."),
    _feedType("sparql-feed", "api-feed", "semantic-web-feed", module="delivery", definition="A semantic data source queried using SPARQL."),
    _feedType("odata-feed", "api-feed", module="delivery", definition="An API feed exposed using OData."),
    _feedType("sdmx-feed", "rest-api-feed", "statistical-data-feed", module="delivery", definition="A statistical feed exposed using SDMX."),
    _feedType("ckan-api-feed", "rest-api-feed", "data-portal-feed", module="delivery", definition="An open-data catalogue feed exposed by CKAN."),
    _feedType("dkan-api-feed", "rest-api-feed", "data-portal-feed", module="delivery", definition="An open-data catalogue feed exposed by DKAN."),
    _feedType("udata-api-feed", "rest-api-feed", "data-portal-feed", module="delivery", definition="An open-data catalogue feed exposed by udata."),
    _feedType("solr-api-feed", "rest-api-feed", module="delivery", definition="A searchable feed exposed through Apache Solr."),
    _feedType("json-api-feed", "rest-api-feed", "json-data-feed", module="delivery", definition="A REST feed represented according to JSON:API."),
    _feedType("oai-pmh-feed", "api-feed", "repository-feed", "xml-data-feed", module="delivery", definition="A repository metadata feed exposed using OAI-PMH."),
    _feedType("iiif-feed", "api-feed", "library-data-feed", module="delivery", definition="A cultural or library source exposed using IIIF."),
    _feedType("entrez-api-feed", "rest-api-feed", "academic-feed", module="delivery", definition="A scholarly source exposed through the NCBI Entrez API."),
    _feedType("soap-api-feed", "api-feed", "xml-data-feed", module="delivery", definition="An API feed exposed using SOAP."),
    _feedType("grpc-feed", "api-feed", "binary-data-feed", module="delivery", definition="An API feed exposed using gRPC."),
    _feedType("xml-data-feed", "feed", module="representation", definition="A feed whose data is represented as XML."),
    _feedType("json-data-feed", "feed", module="representation", definition="A feed whose data is represented as JSON."),
    _feedType("json-ld-feed", "json-data-feed", "semantic-web-feed", module="representation", definition="A linked-data feed represented using JSON-LD."),
    _feedType("ndjson-feed", "json-data-feed", "stream-feed", module="representation", definition="A stream represented as newline-delimited JSON."),
    _feedType("binary-data-feed", "feed", module="representation", definition="A feed represented using a binary serialization."),
    _feedType("tabular-data-feed", "feed", module="representation", definition="A feed whose records are represented in tabular form."),
    _feedType("bulk-data-feed", "feed", module="representation", definition="A feed distributed as one or more bulk data files."),
    _feedType("csv-feed", "tabular-data-feed", "bulk-data-feed", module="representation", definition="A tabular feed distributed as comma-separated values."),
    _feedType("tsv-feed", "tabular-data-feed", "bulk-data-feed", module="representation", definition="A tabular feed distributed as tab-separated values."),
    _feedType("spreadsheet-feed", "tabular-data-feed", "bulk-data-feed", module="representation", definition="A tabular feed distributed as a spreadsheet."),
    _feedType("parquet-feed", "tabular-data-feed", "bulk-data-feed", "binary-data-feed", module="representation", definition="A tabular feed distributed using Apache Parquet."),
    _feedType("arrow-feed", "tabular-data-feed", "binary-data-feed", module="representation", definition="A tabular feed represented using Apache Arrow."),
    _feedType("archive-feed", "bulk-data-feed", "binary-data-feed", module="representation", definition="A bulk feed distributed in an archive or compressed file."),
    _feedType("catalogue-feed", "feed", module="catalogue", definition="A feed that catalogues datasets, records, or other sources."),
    _feedType("web-catalogue-feed", "catalogue-feed", module="catalogue", definition="A catalogue presented primarily through a website or download page."),
    _feedType("data-portal-feed", "catalogue-feed", module="catalogue", definition="A feed exposed by an open-data or dataset portal."),
    _feedType("repository-feed", "catalogue-feed", module="catalogue", definition="A feed of records held by a repository."),
    _feedType("institutional-repository-feed", "repository-feed", "academic-feed", module="catalogue", definition="A feed exposed by an institutional scholarly repository."),
    _feedType("journal-repository-feed", "repository-feed", "academic-feed", module="catalogue", definition="A feed exposed by a journal or publisher repository."),
    _feedType("cultural-repository-feed", "repository-feed", "cultural-data-feed", module="catalogue", definition="A feed exposed by a museum, archive, or cultural repository."),
    _feedType("library-data-feed", "catalogue-feed", module="catalogue", definition="A feed of library, archive, or bibliographic data."),
    _feedType("library-catalogue-feed", "library-data-feed", module="catalogue", definition="A feed exposing a library catalogue."),
    _feedType("feed-directory", "catalogue-feed", module="catalogue", definition="A catalogue whose entries identify other feeds."),
    _feedType("opml-feed", "feed-directory", "xml-data-feed", module="catalogue", definition="A feed directory or outline represented using OPML."),
    _feedType("opds-feed", "atom-feed", "library-catalogue-feed", module="catalogue", definition="A publication catalogue exposed using OPDS."),
    _feedType("sitemap-feed", "xml-data-feed", module="catalogue", definition="A list of discoverable web resources represented as a sitemap."),
    _feedType("semantic-web-feed", "feed", module="semantic", definition="A feed represented or queried using semantic-web standards."),
    _feedType("rdf-feed", "semantic-web-feed", module="semantic", definition="A semantic feed represented using RDF."),
    _feedType("linked-data-feed", "semantic-web-feed", module="semantic", definition="A source exposing linked data."),
    _feedType("statistical-data-feed", "feed", module="domain", definition="A feed of statistical observations or indicators."),
    _feedType("geospatial-feed", "feed", module="domain", definition="A feed of geospatial features, maps, or observations."),
    _feedType("geojson-feed", "geospatial-feed", "json-data-feed", module="domain", definition="A geospatial feed represented using GeoJSON."),
    _feedType("media-feed", "feed", module="domain", definition="A feed that publishes or streams media."),
    _feedType("podcast-feed", "rss-feed", "media-feed", module="domain", definition="A syndication feed that distributes podcast episodes."),
    _feedType("audio-stream-feed", "media-feed", "stream-feed", module="domain", definition="A continuously delivered audio feed."),
    _feedType("video-stream-feed", "media-feed", "stream-feed", module="domain", definition="A continuously delivered video feed."),
    _feedType("hls-feed", "video-stream-feed", module="domain", definition="A video stream delivered using HTTP Live Streaming."),
    _feedType("dash-feed", "video-stream-feed", module="domain", definition="A video stream delivered using MPEG-DASH."),
    _feedType("financial-data-feed", "feed", module="domain", definition="A feed of financial or economic market data."),
    _feedType("market-data-feed", "financial-data-feed", module="domain", definition="A feed of market observations, instruments, or trading data."),
    _feedType("price-feed", "market-data-feed", module="domain", definition="A feed of current or historical prices."),
    _feedType("crypto-price-feed", "price-feed", module="domain", definition="A feed of cryptocurrency prices."),
    _feedType("traditional-asset-price-feed", "price-feed", module="domain", definition="A feed of prices for conventional assets or macroeconomic series."),
    _feedType("news-feed", "feed", module="subject", definition="A feed of news or current-affairs material."),
    _feedType("academic-feed", "feed", module="subject", definition="A feed of scholarly publications, metadata, or research data."),
    _feedType("government-data-feed", "feed", module="subject", definition="A feed published by a government or official statistical body."),
    _feedType("cultural-data-feed", "feed", module="subject", definition="A feed of museum, archive, arts, or cultural-heritage material."),
    _feedType("organization-feed", "feed", module="subject", definition="A feed published by or describing an organization."),
    _feedType("book-data-feed", "library-data-feed", module="subject", definition="A feed of books, publications, or bibliographic records."),
    _feedType("music-data-feed", "media-feed", module="subject", definition="A feed of music metadata, catalogues, or events."),
    _feedType("legislative-data-feed", "government-data-feed", module="subject", definition="A feed of parliamentary or legislative data."),
    _feedType("community-feed", "feed", module="subject", definition="A feed of community posts, discussions, or shared sources."),
    _feedType("newsletter-feed", "community-feed", "syndication-feed", module="subject", definition="A feed associated with a newsletter publication."),
    _feedType("forum-feed", "community-feed", module="subject", definition="A feed of forum or discussion activity."),
)

FEED_TYPE_BY_SLUG = {definition.slug: definition for definition in FEED_TYPE_DEFINITIONS}
FEED_TYPE_REFS = tuple(definition.objectRef for definition in FEED_TYPE_DEFINITIONS)


def validateFeedOntology() -> None:
    coreSlugs = {definition.slug for definition in TYPE_DEFINITIONS}
    feedSlugs = {definition.slug for definition in FEED_TYPE_DEFINITIONS}
    if len(feedSlugs) != len(FEED_TYPE_DEFINITIONS):
        raise ValueError("Feed type slugs must be unique")
    overlap = coreSlugs & feedSlugs
    if overlap:
        raise ValueError(f"Feed types duplicate core types: {sorted(overlap)}")
    validateOntology((*TYPE_DEFINITIONS, *FEED_TYPE_DEFINITIONS))


def _sqlLiteral(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _chunks[T](values: tuple[T, ...], size: int) -> Iterable[tuple[T, ...]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def feedTypeSqlStatements() -> tuple[str, ...]:
    statements: list[str] = []
    for chunk in _chunks(FEED_TYPE_DEFINITIONS, 50):
        rows: list[str] = []
        for definition in chunk:
            knowledge = {
                "definition": definition.definition,
                "module": definition.module,
                "ontology": "syn-feeds",
                "ontologyVersion": FEED_ONTOLOGY_VERSION,
                "derivedFromDatasets": [
                    "feed_mime_types_117.csv",
                    "feed_search_queries_313.csv",
                    MASTER_DATASET,
                ],
            }
            rows.append(
                "("
                + ", ".join(
                    (
                        f'{_sqlLiteral(definition.objectRef)}::"typeRef"',
                        _sqlLiteral(definition.name),
                        f"{_sqlLiteral(json.dumps(knowledge, ensure_ascii=False))}::jsonb",
                    )
                )
                + ")"
            )
        statements.append(
            "INSERT INTO types (id, name, knowledge) VALUES\n    "
            + ",\n    ".join(rows)
            + "\nON CONFLICT (id) DO UPDATE SET "
            "name = EXCLUDED.name, "
            "knowledge = coalesce(types.knowledge, '{}'::jsonb) || EXCLUDED.knowledge"
        )

    edges = tuple(
        (definition.objectRef, f"syn://type/{parent}")
        for definition in FEED_TYPE_DEFINITIONS
        for parent in definition.parents
    )
    for chunk in _chunks(edges, 100):
        rows = [
            f'({_sqlLiteral(child)}::"typeRef", {_sqlLiteral(parent)}::"typeRef")'
            for child, parent in chunk
        ]
        statements.append(
            'INSERT INTO "typeParents" ("childRef", "parentRef") VALUES\n    '
            + ",\n    ".join(rows)
            + '\nON CONFLICT ("childRef", "parentRef") DO NOTHING'
        )
    return tuple(statements)


@dataclass(frozen=True, slots=True)
class FeedRecord:
    objectRef: ObjectRef
    name: str
    canonicalUrl: str
    typeSlugs: tuple[str, ...]
    knowledge: dict[str, object]


@dataclass(frozen=True, slots=True)
class FeedImportPlan:
    sourceCount: int
    records: tuple[FeedRecord, ...]

    @property
    def uniqueCount(self) -> int:
        return len(self.records)

    @property
    def duplicateCount(self) -> int:
        return self.sourceCount - self.uniqueCount


@dataclass(frozen=True, slots=True)
class FeedImportResult:
    sourceCount: int
    uniqueCount: int
    duplicateCount: int
    insertedCount: int
    updatedCount: int
    classificationCount: int


def _canonicalNetloc(parsed: SplitResult) -> str:
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("Feed URL must include a host")
    hostname = hostname.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Feed URL contains an invalid port") from error
    if port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"

    userInfo = ""
    if parsed.username is not None:
        userInfo = parsed.username
        if parsed.password is not None:
            userInfo += f":{parsed.password}"
        userInfo += "@"
    return userInfo + hostname


def canonicalFeedUrl(value: str) -> str:
    cleaned = value.strip()
    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Feed URL must use http or https: {value!r}")
    path = parsed.path
    if path == "/":
        path = ""
    return urlunsplit((scheme, _canonicalNetloc(parsed), path, parsed.query, ""))


def _queryRequirement(value: str | None) -> bool | None:
    normalized = (value or "").strip().lower()
    if normalized.startswith("yes"):
        return True
    if normalized.startswith("no"):
        return False
    return None


def classifyFeed(row: Mapping[str, str | None]) -> tuple[str, ...]:
    types = {"feed"}
    category = (row.get("Category") or "").casefold()
    subcategory = (row.get("Subcategory") or "").casefold()
    protocol = (row.get("Protocol / standard") or "").casefold()
    dataFormat = (row.get("Data format / MIME") or "").casefold()
    urlForm = (row.get("URL form") or "").casefold()
    combined = " | ".join((subcategory, protocol, dataFormat, urlForm))

    categoryTypes = {
        "news and current affairs": "news-feed",
        "academic and research": "academic-feed",
        "government and official statistics": "government-data-feed",
        "arts, museums and cultural heritage": "cultural-data-feed",
        "price and market data": "price-feed",
        "ngos, unions, guilds and international organisations": "organization-feed",
        "books, publishing and libraries": "book-data-feed",
        "music and audio": "music-data-feed",
        "parliaments, national assemblies and civic institutions": "legislative-data-feed",
        "community, newsletters and historical web feeds": "community-feed",
    }
    if category in categoryTypes:
        types.add(categoryTypes[category])

    subcategoryRules = (
        ("publisher syndication", ("syndication-feed",)),
        ("institutional repository", ("institutional-repository-feed",)),
        ("journal/publisher repository", ("journal-repository-feed",)),
        ("library, archive or cultural repository", ("cultural-repository-feed", "library-data-feed")),
        ("national statistical office", ("statistical-data-feed", "data-portal-feed")),
        ("national open-data catalogue", ("data-portal-feed",)),
        ("scholarly metadata", ("academic-feed", "repository-feed")),
        ("crypto prices", ("crypto-price-feed",)),
        ("traditional assets", ("traditional-asset-price-feed",)),
        ("organisational data", ("organization-feed",)),
        ("books / bibliographic", ("book-data-feed",)),
        ("collections / creative", ("cultural-data-feed",)),
        ("music metadata", ("music-data-feed",)),
        ("legislative data", ("legislative-data-feed",)),
        ("substack", ("newsletter-feed",)),
        ("reddit", ("forum-feed",)),
        ("forums", ("forum-feed",)),
        ("catalogue api", ("catalogue-feed", "api-feed")),
    )
    for marker, slugs in subcategoryRules:
        if marker in subcategory:
            types.update(slugs)

    protocolRules = (
        ("podcast rss", ("podcast-feed",)),
        ("oai-pmh", ("oai-pmh-feed",)),
        ("json:api", ("json-api-feed",)),
        ("graphql", ("graphql-feed",)),
        ("sparql", ("sparql-feed",)),
        ("sdmx", ("sdmx-feed",)),
        ("ckan", ("ckan-api-feed",)),
        ("dkan", ("dkan-api-feed",)),
        ("udata", ("udata-api-feed",)),
        ("solr", ("solr-api-feed",)),
        ("odata", ("odata-feed",)),
        ("iiif", ("iiif-feed",)),
        ("entrez", ("entrez-api-feed",)),
        ("soap", ("soap-api-feed",)),
        ("grpc", ("grpc-feed",)),
        ("opds", ("opds-feed",)),
        ("opml", ("opml-feed",)),
        ("sitemap", ("sitemap-feed",)),
        ("linked data", ("linked-data-feed",)),
        ("rdf", ("rdf-feed",)),
        ("websocket", ("websocket-feed",)),
        ("server-sent", ("server-sent-events-feed",)),
        ("webhook", ("webhook-feed",)),
        ("hls", ("hls-feed",)),
        ("mpeg-dash", ("dash-feed",)),
        ("exchange rest", ("rest-api-feed", "market-data-feed")),
        ("crypto aggregator rest", ("rest-api-feed", "crypto-price-feed")),
        ("market rest", ("rest-api-feed", "market-data-feed")),
        ("rest", ("rest-api-feed",)),
        ("atom api", ("atom-feed", "api-feed")),
        ("atom", ("atom-feed",)),
        ("rss", ("rss-feed",)),
        ("syndication", ("syndication-feed",)),
        ("bulk download", ("bulk-data-feed",)),
        ("web catalogue", ("web-catalogue-feed",)),
        ("open-data catalogue", ("data-portal-feed",)),
        ("catalogue", ("catalogue-feed",)),
        ("search/data service", ("api-feed",)),
        ("csv query", ("api-feed", "csv-feed")),
    )
    for marker, slugs in protocolRules:
        if marker in protocol:
            types.update(slugs)

    formatRules = (
        ("application/rss+xml", ("rss-feed",)),
        ("application/atom+xml", ("atom-feed",)),
        ("json-ld", ("json-ld-feed",)),
        ("geojson", ("geojson-feed",)),
        ("ndjson", ("ndjson-feed",)),
        ("parquet", ("parquet-feed",)),
        ("arrow", ("arrow-feed",)),
        ("spreadsheet", ("spreadsheet-feed",)),
        ("excel", ("spreadsheet-feed",)),
        ("text/csv", ("csv-feed",)),
        ("csv", ("csv-feed",)),
        ("tsv", ("tsv-feed",)),
        ("zip", ("archive-feed",)),
        ("gzip", ("archive-feed",)),
        ("tar", ("archive-feed",)),
        ("application/json", ("json-data-feed",)),
        ("json", ("json-data-feed",)),
        ("text/xml", ("xml-data-feed",)),
        ("application/xml", ("xml-data-feed",)),
        ("xml", ("xml-data-feed",)),
    )
    for marker, slugs in formatRules:
        if marker in dataFormat:
            types.update(slugs)

    if "direct syndication endpoint" in urlForm:
        types.add("syndication-feed")
    if "oai-pmh base endpoint" in urlForm:
        types.add("oai-pmh-feed")
    if "api" in urlForm:
        types.add("api-feed")
    if "portal" in urlForm:
        types.add("data-portal-feed")
    if "catalogue" in combined or "catalog" in combined:
        types.add("catalogue-feed")
    if "statistic" in combined:
        types.add("statistical-data-feed")

    unknown = types - FEED_TYPE_BY_SLUG.keys()
    if unknown:
        raise ValueError(f"Classifier produced unknown feed types: {sorted(unknown)}")
    return tuple(sorted(types))


def _cleanCell(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _description(row: Mapping[str, str | None]) -> str:
    source = _cleanCell(row.get("Source name")) or _cleanCell(row.get("Publishing entity")) or "This feed"
    publisher = _cleanCell(row.get("Publishing entity"))
    subcategory = _cleanCell(row.get("Subcategory")) or "data feed"
    category = _cleanCell(row.get("Category"))
    geography = _cleanCell(row.get("Geography"))
    language = _cleanCell(row.get("Language"))
    protocol = _cleanCell(row.get("Protocol / standard"))
    dataFormat = _cleanCell(row.get("Data format / MIME"))
    authentication = _cleanCell(row.get("Authentication"))
    validation = _cleanCell(row.get("Validation status"))

    description = f"{source} is a {subcategory.lower()}"
    if publisher and publisher.casefold() != source.casefold():
        description += f" published by {publisher}"
    if category:
        description += f" for {category.lower()}"
    scope = ""
    if geography:
        scope += f" in {geography}"
    if language:
        scope += f" in {language}"
    description += scope
    if protocol:
        description += f", delivered via {protocol}"
    if dataFormat:
        description += f" as {dataFormat}"

    queryRequired = _queryRequirement(row.get("API query required"))
    if queryRequired is True:
        description += ", requiring an API query"
    elif queryRequired is False:
        description += ", directly retrievable without an API query"
    else:
        description += ", with query requirements dependent on use"

    if authentication and not authentication.casefold().startswith("none"):
        description += f" and authentication described as {authentication}"
    if validation:
        description += f"; its recorded validation status is {validation}"
    return description.rstrip(". ") + "."


def _feedObjectRef(canonicalUrl: str) -> ObjectRef:
    host = urlsplit(canonicalUrl).hostname or "source"
    hostSlug = normalizeSlug(host)[:100] or "source"
    digest = hashlib.sha256(canonicalUrl.encode("utf-8")).hexdigest()[:24]
    return ObjectRef.local("thing", f"feed-{hostSlug}-{digest}")


def _readMasterRows(bundlePath: str | Path) -> list[dict[str, str]]:
    path = Path(bundlePath)
    with ZipFile(path) as bundle:
        matches = [name for name in bundle.namelist() if Path(name).name == MASTER_DATASET]
        if len(matches) != 1:
            raise ValueError(f"Bundle must contain exactly one {MASTER_DATASET}; found {len(matches)}")
        with bundle.open(matches[0]) as rawFile:
            with io.TextIOWrapper(rawFile, encoding="utf-8-sig", newline="") as textFile:
                reader = csv.DictReader(textFile)
                actual = tuple(reader.fieldnames or ())
                if actual != MASTER_COLUMNS:
                    raise ValueError(
                        f"Unexpected {MASTER_DATASET} columns; expected {MASTER_COLUMNS}, got {actual}"
                    )
                return [dict(row) for row in reader]


def _preferredRow(rows: list[tuple[int, dict[str, str]]]) -> tuple[int, dict[str, str]]:
    return min(
        rows,
        key=lambda item: (
            (urlsplit(item[1]["Data source URL"].strip()).hostname or "").casefold().startswith("www."),
            item[0],
        ),
    )


def _mergeRows(rows: list[tuple[int, dict[str, str]]]) -> tuple[dict[str, str], dict[str, list[str]]]:
    _, preferred = _preferredRow(rows)
    merged = dict(preferred)
    alternateValues: dict[str, list[str]] = {}
    for column in MASTER_COLUMNS:
        values: list[str] = []
        for _, row in rows:
            cleaned = (row[column] or "").strip()
            if cleaned and cleaned not in values:
                values.append(cleaned)
        if not (merged[column] or "").strip() and values:
            merged[column] = values[0]
        if len(values) > 1 and column != "Data source URL":
            alternateValues[KNOWLEDGE_FIELDS[column]] = values
    return merged, alternateValues


def buildFeedImportPlan(bundlePath: str | Path) -> FeedImportPlan:
    rows = _readMasterRows(bundlePath)
    groups: dict[str, list[tuple[int, dict[str, str]]]] = defaultdict(list)
    for rowNumber, row in enumerate(rows, start=2):
        sourceUrl = (row["Data source URL"] or "").strip()
        if not sourceUrl:
            raise ValueError(f"{MASTER_DATASET} row {rowNumber} has no Data source URL")
        try:
            key = canonicalFeedUrl(sourceUrl)
        except ValueError as error:
            raise ValueError(f"{MASTER_DATASET} row {rowNumber}: {error}") from error
        groups[key].append((rowNumber, row))

    records: list[FeedRecord] = []
    for canonicalUrl, groupedRows in groups.items():
        merged, alternateValues = _mergeRows(groupedRows)
        typeSlugs = classifyFeed(merged)
        knowledge: dict[str, object] = {
            target: _cleanCell(merged.get(source))
            for source, target in KNOWLEDGE_FIELDS.items()
        }
        sourceUrls = list(
            dict.fromkeys(
                (row["Data source URL"] or "").strip() for _, row in groupedRows
            )
        )
        knowledge.update(
            {
                "canonicalUrl": canonicalUrl,
                "description": _description(merged),
                "duplicateUrls": sourceUrls if len(groupedRows) > 1 else [],
                "feedTypeRefs": [f"syn://type/{slug}" for slug in typeSlugs],
                "requiresApiQuery": _queryRequirement(merged.get("API query required")),
                "sourceDataset": MASTER_DATASET,
                "sourceRowNumbers": [rowNumber for rowNumber, _ in groupedRows],
            }
        )
        if alternateValues:
            knowledge["alternateValues"] = alternateValues

        name = (
            _cleanCell(merged.get("Source name"))
            or _cleanCell(merged.get("Publishing entity"))
            or urlsplit(canonicalUrl).hostname
            or canonicalUrl
        )
        records.append(
            FeedRecord(
                objectRef=_feedObjectRef(canonicalUrl),
                name=name,
                canonicalUrl=canonicalUrl,
                typeSlugs=typeSlugs,
                knowledge=knowledge,
            )
        )

    records.sort(key=lambda record: record.canonicalUrl)
    objectRefs = [record.objectRef for record in records]
    if len(set(objectRefs)) != len(objectRefs):
        raise ValueError("Generated feed ObjectRefs are not unique")
    return FeedImportPlan(sourceCount=len(rows), records=tuple(records))


def importFeedPlan(connection: Connection, plan: FeedImportPlan) -> FeedImportResult:
    objectRefs = tuple(str(record.objectRef) for record in plan.records)
    existing: set[str] = set()
    for chunk in _chunks(objectRefs, 500):
        existing.update(
            connection.scalars(select(Thing.id).where(Thing.id.in_(chunk)))
        )

    typeRefsByCombination: dict[tuple[str, ...], str] = {}
    for typeSlugs in sorted({record.typeSlugs for record in plan.records}):
        typeRefsByCombination[typeSlugs] = str(
            connection.scalar(
                text(
                    'SELECT "resolveTypeIntersection"('
                    'CAST(:parents AS "typeRef"[]))'
                ),
                {"parents": [f"syn://type/{slug}" for slug in typeSlugs]},
            )
        )

    for chunk in _chunks(plan.records, 250):
        rows = [
            {
                "id": str(record.objectRef),
                "typeRef": typeRefsByCombination[record.typeSlugs],
                "name": record.name,
                "knowledge": record.knowledge,
            }
            for record in chunk
        ]
        statement = postgresqlInsert(Thing.__table__).values(rows)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[Thing.id],
                set_={
                    "typeRef": statement.excluded.typeRef,
                    "name": statement.excluded.name,
                    "knowledge": func.coalesce(
                        Thing.knowledge, literal_column("'{}'::jsonb")
                    )
                    .op("||")(statement.excluded.knowledge),
                    "knowledgeUpdated": func.now(),
                },
            )
        )

    insertedCount = len(objectRefs) - len(existing)
    return FeedImportResult(
        sourceCount=plan.sourceCount,
        uniqueCount=plan.uniqueCount,
        duplicateCount=plan.duplicateCount,
        insertedCount=insertedCount,
        updatedCount=len(existing),
        classificationCount=len(plan.records),
    )


def importFeedBundle(connection: Connection, bundlePath: str | Path) -> FeedImportResult:
    return importFeedPlan(connection, buildFeedImportPlan(bundlePath))


validateFeedOntology()
