# Syn

Syn is a uv-managed Python package containing SQLAlchemy 2 models for the Syn PostgreSQL schema.

## Setup

```sh
uv sync
```

## Usage

Copy `config.example.toml` to `config.toml` and set the real PostgreSQL connection string. `config.toml` is ignored by Git.

```python
from syn import ObjectRef, Thing, createEngine, createSessionFactory, loadConfig, sessionScope

config = loadConfig()
engine = createEngine(
    config.database.url,
    echo=config.database.echo,
    migrate=config.database.autoMigrate,
)
sessions = createSessionFactory(engine)

with sessionScope(sessions) as session:
    session.add(
        Thing(
            id=ObjectRef.local("thing", "example"),
            typeRef=ObjectRef.local("type", "thing"),
            name="Example",
        )
    )
```

Run `database/objectReferenceSchema.sql` before using the models. Missing local references used by the linking tables are silently materialized by PostgreSQL as placeholder records containing their ObjectRef and a slug-derived name. General external ObjectRefs are not materialized.

## Migrations

`createEngine()` automatically upgrades the database to the latest Alembic revision by default. Set `database.autoMigrate = false` in `config.toml` or pass `migrate=False` only when migrations are managed by a separate deployment process.

The initial migration packages the full domain/function/trigger schema. A complete unversioned Syn schema is validated and stamped automatically; a partial unversioned schema is rejected.

The next migration installs Syn's polyhierarchical core ontology. Types may have
multiple parents through `typeParents`, but `subTypeOf` cycles are rejected. The
`typeAncestry` and `objectTypeAncestry` views expose transitive inheritance,
and `objectHasType()` checks both direct and inherited membership. Every Thing,
Event, Ember, Cog, Enigma, Pondle, Snug, and Om has exactly one required
`typeRef` directly on its table. The former `objectTypes` linking table has been
removed. Each primitive has a root Type such as `syn://type/enigma`; missing
classifications fall back to the appropriate primitive root during migration
or placeholder materialization.

The curated ontology is defined in `syn.ontology` and seeded idempotently during
migration. Its `question` type retains the project definition: “A proposition
which can be answered, either yes or no, or with any other kind of response.”

The feed ontology is defined in `syn.feeds`. It supports overlapping subject,
delivery, representation, catalogue, media, statistical, geospatial, and market
feed types. Imported Things receive one direct `typeRef`; when several
independent classifications apply, Syn reuses or creates a validated
intersection type and gains the parent classifications through inheritance.

Create and review a future ORM migration:

```sh
uv run alembic revision --autogenerate -m "describe change"
```

Apply migrations explicitly:

```sh
uv run alembic upgrade head
```

Alembic can autogenerate table, column, index, and ordinary constraint changes. PostgreSQL domains, functions, and triggers must be added manually to revisions and reviewed before deployment.

## Feed import

Import the global feed research CSV bundle with the project configuration:

```sh
uv run python scripts/importFeeds.py "C:\path\to\global_feed_research_csv_bundle.zip"
```

The importer reads `global_data_sources_master_2326.csv`, treats the companion
CSV files as ontology research rather than additional feed rows, and merges URL
duplicates after safe URL normalization. It preserves every master column in
the Thing's camelCase `knowledge` dictionary, adds a description and provenance,
and upserts deterministic `syn://thing/` ObjectRefs. Re-running it is safe.

Verify an installed feed dataset:

```sh
uv run python scripts/verifyFeeds.py
```

## Test

Run the complete suite. PostgreSQL integration tests read their connection URL
from `config.toml` and skip until it contains a usable URL. Set `SYN_CONFIG` to
select a different configuration file when needed:

```sh
uv run pytest
```

Run the fast, database-independent suite with Python branch coverage:

```sh
uv run pytest -m "not database" --cov=syn --cov-report=term-missing
```

Run only the PostgreSQL domain, constraint, trigger, and index tests:

```sh
uv run pytest -m database
```

## API service

Start the web server with the uv command installed by the package:

```sh
uv run syn-server
```

The equivalent direct script is `uv run python scripts/startServer.py`. Both
load `config.toml`, apply migrations when `database.autoMigrate` is enabled,
register the default process handlers, and serve on `http://127.0.0.1:8000`.
Use `--host`, `--port`, `--config`, and `--log-level` to override the defaults:

```sh
uv run syn-server --host 0.0.0.0 --port 8080 --log-level debug
```

Open `http://127.0.0.1:8000/` to view the Jinja-rendered Syn welcome page.
Open `http://127.0.0.1:8000/editTypes` to search for a type and load its
database-backed details into an editable form and save changes.

The FastAPI application exposes placeholder `GET` controllers for each core
database primitive while their persistence behavior is developed:

- `GET /api/cog`
- `GET /api/ember`
- `GET /api/enigma`
- `GET /api/event`
- `GET /api/om`
- `GET /api/pondle`
- `GET /api/snug`
- `GET /api/thing`

Each currently returns the primitive name, a `placeholder` status, and a
temporary response message. They are included in the generated OpenAPI schema.

Types additionally expose database-backed endpoints:

- `GET /api/type/{id}` retrieves `syn://type/{id}` with its recursively nested
  parent-type branches.
- `PUT /api/type/{id}` updates the existing `syn://type/{id}` record.
- `GET /api/type?query={search}` searches type names and IDs and returns up to
  ten relevance-ranked results.

When `parents` is supplied to the PUT endpoint, it must be a list of local Type
ObjectRefs. The Type model rejects missing types, duplicates, self-parenting,
and any list in which one selected parent is an ancestor of another selected
parent at any depth. Omitting `parents` leaves the existing relationships intact.

### Process scheduler

Create an application with an explicit allow-list of process-safe handlers. Handler
functions must be module-level callables so Python can pickle them for a child process.

```python
from syn.api import createApp

def buildReport(reportId: str) -> dict:
    return {"reportId": reportId, "complete": True}

app = createApp(sessions, {"build-report": buildReport})
```

Run that module with Uvicorn, then use `POST /scheduler/start`, `POST /jobs`,
`GET /jobs/{jobId}`, `DELETE /jobs/{jobId}`, and
`GET /jobs/{jobId}/result`. A job submission body has the form
`{"handler": "build-report", "arguments": {"reportId": "example"}}`.

Syn also supplies `defaultJobHandlers`, including `witness-stale-feeds`. This
job selects the 100 feed Things least recently witnessed, scores their stored
activity, and witnesses the most active 50 in descending score order:

```python
from syn import defaultJobHandlers

app = createApp(sessions, defaultJobHandlers(config.database.url))
```

Submit it with `{"handler":"witness-stale-feeds","arguments":{}}`. The database
URL is bound when the application is created and is not persisted in job arguments.
Each successful request creates a `feedWitness` Event containing the feed and
URL references, response bytes, parsed entry timestamps, HTTP headers, answering
IP address, and DNS/TCP/TLS/HTTP/data-transfer/connection-close timings. Only
publicly routed destination addresses are allowed, and responses default to a
10 MB limit. HTTP 301 and 302 redirects are followed for up to ten hops, with
destination safety checks repeated for every hop. The job prints timestamped
actions while it runs and includes those logs and per-hop timings in its result.
TLS verification uses the checksum-pinned Mozilla CA bundle packaged at
`src/syn/cacert.pem`; its upstream checksum is retained alongside it.

In persistence-enabled environments, a successful witness creates an Event
classified as `observation`, an Ember classified as `observation-result` holding
the redacted HTTP result, and a separate `network-measurement` Ember holding network
timings and transfer statistics. Event targets and object relationships connect
all three objects. Authorization, cookie, proxy-authorization, and API-key
headers are replaced with `[REDACTED]` before objects are constructed.
The network measurement records the peer certificate chain for every TLS hop,
including X.509 metadata, fingerprints, signature algorithms, and PEM-encoded
public keys.
