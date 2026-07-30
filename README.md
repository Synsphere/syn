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
`typeAncestry` and `objectTypesInherited` views expose transitive inheritance,
and `objectHasType()` checks both direct and inherited membership.

The curated ontology is defined in `syn.ontology` and seeded idempotently during
migration. Its `question` type retains the project definition: “A proposition
which can be answered, either yes or no, or with any other kind of response.”

The feed ontology is defined in `syn.feeds`. It supports overlapping subject,
delivery, representation, catalogue, media, statistical, geospatial, and market
feed types; imported Things receive direct types through `objectTypes` and gain
their parent types through the normal inheritance graph.

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

Run the complete suite. PostgreSQL integration tests skip until `config.toml` has a usable URL or `SYN_TEST_DATABASE_URL` is set:

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
