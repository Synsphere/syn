from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine, text

from syn import loadConfig


EXPECTED_EVENTS = 47
EXPECTED_THINGS = 94


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    engine = create_engine(loadConfig().database.url)
    with engine.begin() as connection:
        rows = connection.execute(text('''
            SELECT event.id::text AS "eventRef",
                   event.knowledge->>'observationRef' AS "observationRef",
                   event.knowledge->>'networkStatsRef' AS "networkStatsRef"
            FROM events event
            WHERE event.name = 'feedWitness'
              AND event.knowledge->>'observationRef' LIKE 'syn://thing/%'
              AND event.knowledge->>'networkStatsRef' LIKE 'syn://thing/%'
            ORDER BY event.id
        ''')).mappings().all()
        mapping: dict[str, str] = {}
        for row in rows:
            for key in ("observationRef", "networkStatsRef"):
                old = row[key]
                mapping[old] = old.replace("syn://thing/", "syn://ember/", 1)

        thing_count = connection.scalar(
            text('SELECT count(*) FROM things WHERE id::text = ANY(:refs)'),
            {"refs": list(mapping)},
        )
        audit = {
            "events": len(rows),
            "mappedThings": len(mapping),
            "existingThings": thing_count,
            "mode": "apply" if arguments.apply else "audit",
        }
        print(json.dumps(audit, indent=2), flush=True)
        if len(rows) != EXPECTED_EVENTS or len(mapping) != EXPECTED_THINGS:
            raise RuntimeError(f"Expected {EXPECTED_EVENTS} events and {EXPECTED_THINGS} Things")
        if thing_count != EXPECTED_THINGS:
            raise RuntimeError(f"Expected all {EXPECTED_THINGS} legacy Things to exist")

        unexpected = connection.execute(text('''
            SELECT old_ref, source
            FROM (
                SELECT target."targetRef"::text AS old_ref, 'eventTargets' AS source
                FROM "eventTargets" target
                WHERE target."targetRef"::text = ANY(:refs)
                  AND target.role NOT IN ('observation-result', 'network-stats')
                UNION ALL
                SELECT chronicle."objectRef"::text, 'objectChronicle'
                FROM "objectChronicle" chronicle
                WHERE chronicle."objectRef"::text = ANY(:refs)
            ) AS found_references
        '''), {"refs": list(mapping)}).mappings().all()
        if unexpected:
            raise RuntimeError(f"Unexpected references prevent migration: {unexpected}")

        if not arguments.apply:
            connection.rollback()
            return

        for old_ref, new_ref in mapping.items():
            connection.execute(text('''
                INSERT INTO embers (
                    id, "typeRef", name, locality, stats, "statsUpdated", knowledge,
                    "knowledgeUpdated", confidence, "confidenceUpdated"
                )
                SELECT CAST(:new_ref AS "emberRef"), "typeRef", name, locality, stats,
                       "statsUpdated", knowledge, "knowledgeUpdated", confidence,
                       "confidenceUpdated"
                FROM things
                WHERE id = CAST(:old_ref AS "thingRef")
            '''), {"old_ref": old_ref, "new_ref": new_ref})

        for row in rows:
            observation_ref = mapping[row["observationRef"]]
            network_ref = mapping[row["networkStatsRef"]]
            connection.execute(text('''
                UPDATE events
                SET knowledge = jsonb_set(
                    jsonb_set(knowledge, '{observationRef}', to_jsonb(CAST(:observation AS text))),
                    '{networkStatsRef}', to_jsonb(CAST(:network AS text))
                )
                WHERE id = CAST(:event_ref AS "eventRef")
            '''), {
                "event_ref": row["eventRef"],
                "observation": observation_ref,
                "network": network_ref,
            })

        for old_ref, new_ref in mapping.items():
            connection.execute(text('''
                UPDATE "eventTargets" SET "targetRef" = CAST(:new_ref AS "objectRef")
                WHERE "targetRef" = CAST(:old_ref AS "objectRef")
            '''), {"old_ref": old_ref, "new_ref": new_ref})
            connection.execute(text('''
                UPDATE "objectRelationships" SET "sourceRef" = CAST(:new_ref AS "objectRef")
                WHERE "sourceRef" = CAST(:old_ref AS "objectRef")
            '''), {"old_ref": old_ref, "new_ref": new_ref})
            connection.execute(text('''
                UPDATE "objectRelationships" SET "targetRef" = CAST(:new_ref AS "objectRef")
                WHERE "targetRef" = CAST(:old_ref AS "objectRef")
            '''), {"old_ref": old_ref, "new_ref": new_ref})
            connection.execute(
                text('DELETE FROM things WHERE id = CAST(:old_ref AS "thingRef")'),
                {"old_ref": old_ref},
            )

        counts = connection.execute(text('''
            SELECT
                (SELECT count(*) FROM things WHERE id::text = ANY(:old_refs)) AS old_things,
                (SELECT count(*) FROM embers WHERE id::text = ANY(:new_refs)) AS new_embers,
                (SELECT count(*) FROM events
                 WHERE name = 'feedWitness'
                   AND knowledge->>'observationRef' LIKE 'syn://ember/%'
                   AND knowledge->>'networkStatsRef' LIKE 'syn://ember/%') AS migrated_events
        '''), {"old_refs": list(mapping), "new_refs": list(mapping.values())}).mappings().one()
        if dict(counts) != {
            "old_things": 0,
            "new_embers": EXPECTED_THINGS,
            "migrated_events": EXPECTED_EVENTS,
        }:
            raise RuntimeError(f"Post-migration verification failed: {dict(counts)}")
        print(json.dumps(dict(counts), indent=2), flush=True)


if __name__ == "__main__":
    main()
