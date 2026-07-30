# Syn database

This directory contains the PostgreSQL schema and the generated Wikipedia Thing seed.

The application packages this schema as the baseline Alembic migration and normally applies it automatically through `createEngine()`. The direct `psql` instructions below remain useful for manual administration and inspection.

## Files

- `objectReferenceSchema.sql` creates the domains, tables, functions, triggers, constraints, and indexes.
- `seeds/populateThings.sql.gz` contains 820,433 batched, re-runnable Thing upserts.

`objectReferenceSchema.sql` is the baseline schema. Alembic migrations add the
polyhierarchical `typeParents` graph, inherited-type views and functions, and
the curated Syn ontology. The feed migration adds the polyhierarchical feed-type
ontology; feed data itself is loaded with `scripts/importFeeds.py` so the source
bundle remains external to the repository.

The seed maps each JSONL entity as follows:

- `id`: `syn://thing/{normalized-title-slug}`
- `name`: Wikipedia title
- `wikipediaRef`: `https://en.wikipedia.org/?curid={wikipedia_id}`
- `knowledge`: JSONB containing `wikipediaId` and `description`

The generator resolved 606 normalized-slug collisions by appending the Wikipedia ID. No input lines were invalid.

## Load

Create the schema first:

```sh
psql --set ON_ERROR_STOP=1 --dbname "$DATABASE_URL" --file database/objectReferenceSchema.sql
uv run alembic upgrade head
```

Then stream the compressed seed into PostgreSQL:

```sh
gzip --decompress --stdout database/seeds/populateThings.sql.gz |
  psql --set ON_ERROR_STOP=1 --dbname "$DATABASE_URL"
```

The seed runs in one transaction and uses `ON CONFLICT (id) DO UPDATE`, so it can be run again safely. Allow sufficient transaction-log and disk capacity for the 820,433-row import.

## Missing local references

When a linking-table write uses a valid local `syn://` ObjectRef that is not yet present, the schema silently creates a placeholder in the corresponding model table. The placeholder contains the ObjectRef plus a `name` derived from its slug; optional fields remain null. External and relative ObjectRefs are never materialized locally.

## Integrity

SHA-256 checksums:

```text
objectReferenceSchema.sql
0AC91B33E0D03828492C913F81D583351516E89487212B171621C02616747D6E

populateThings.sql.gz
32A75083CF0B5A9D4D99A4019A1E6FB77827F3D39901BC3BE3DC4C7B221AE11D
```
