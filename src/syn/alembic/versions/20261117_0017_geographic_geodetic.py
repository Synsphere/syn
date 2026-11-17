"""Correct the geographic type name without rewriting position history.

Keep 0016 frozen. Only un-hashed ontology/frame metadata changes here. Existing
Events, Embers, canonical pointers, snapshots and receipts are left intact.
On restart the runtime appends a new Ember for each legacy canonical position.

Stop position writers during deployment. PostgreSQL transactional DDL and the
exclusive frame lock make the temporary trigger replacement all-or-nothing.
"""
from alembic import op

revision = "20261117_0017"
down_revision = "20261117_0016"
branch_labels = None
depends_on = None

# Frozen SQL: never import the changing runtime ontology into a migration.
STATEMENTS = (
    "SELECT pg_advisory_xact_lock(hashtextextended('syn.position.spaces',0))",
    'LOCK TABLE "coordinateSpaces" IN ACCESS EXCLUSIVE MODE',
    "SELECT pg_advisory_xact_lock(hashtextextended('syn.typeParents',0))",
    'DO $$ BEGIN\n  IF EXISTS (SELECT 1 FROM types WHERE id=\'syn://type/geographic-geodetic\'\n             AND knowledge->>\'ontology\' IS DISTINCT FROM \'syn-position\') THEN\n    RAISE EXCEPTION \'The geographic-geodetic identifier is already used by an unrelated Type\';\n  END IF;\n  IF EXISTS (SELECT 1 FROM "coordinateSpaces" s\n             WHERE s."spaceRef"=\'syn://thing/space-wgs84-geodetic\' AND (\n               s."typeRef" NOT IN (\'syn://type/geographic-cartesian\',\'syn://type/geographic-geodetic\') OR\n               (s."typeRef"=\'syn://type/geographic-cartesian\' AND s.definition::jsonb IS DISTINCT FROM \'{"axes": ["latitude", "longitude", "altitude"], "description": "WGS84 geodetic position; altitude datum is specified by each estimate", "errorUnit": "metre", "metric": "wgs84-horizontal", "reference": "syn://thing/space-wgs84-geodetic", "typeRef": "syn://type/geographic-cartesian", "units": ["degree", "degree", "metre"]}\'::jsonb)\n             )) THEN\n    RAISE EXCEPTION \'Unexpected WGS84 frame definition; refusing a terminology-only migration\';\n  END IF;\n  IF EXISTS (SELECT 1 FROM "coordinateSpaces" s JOIN things t ON t.id::text=s."spaceRef"\n             WHERE s."spaceRef"=\'syn://thing/space-wgs84-geodetic\' AND s."typeRef"=\'syn://type/geographic-cartesian\'\n               AND (t."typeRef"::text<>\'syn://type/coordinate-space\'\n                    OR t.knowledge::jsonb IS DISTINCT FROM s.definition::jsonb)) THEN\n    RAISE EXCEPTION \'WGS84 Thing and registered frame disagree\';\n  END IF;\nEND $$',
    'INSERT INTO types (id,name,knowledge) VALUES\n    (\'syn://type/geographic-geodetic\',\'geographic-geodetic\',\'{"definition": "WGS84 geodetic latitude/longitude in decimal degrees and altitude in metres with an explicit altitude datum; not Cartesian XYZ.", "ontology": "syn-position", "ontologyVersion": 2}\'::jsonb)\n    ON CONFLICT (id) DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES\n    (\'syn://type/geographic-geodetic\',\'syn://type/space-position-estimate\') ON CONFLICT DO NOTHING',
    'DELETE FROM "typeParents" WHERE "parentRef"=\'syn://type/geographic-cartesian\'\n    AND "childRef" IN (\'syn://type/gps-reading\',\'syn://type/os-location-reading\')',
    'DELETE FROM "typeParents" WHERE "childRef"=\'syn://type/geographic-cartesian\'\n    AND "parentRef"=\'syn://type/space-position-estimate\'',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES\n    (\'syn://type/geographic-cartesian\',\'syn://type/geographic-geodetic\'),\n    (\'syn://type/gps-reading\',\'syn://type/geographic-geodetic\'),\n    (\'syn://type/os-location-reading\',\'syn://type/geographic-geodetic\') ON CONFLICT DO NOTHING',
    'UPDATE types SET name=\'geographic-geodetic (legacy alias)\',\n    knowledge=coalesce(knowledge,\'{}\'::jsonb) || \'{"definition": "Deprecated compatibility identifier for immutable historical geographic-geodetic records; do not use for new estimates.", "deprecated": true, "ontology": "syn-position", "ontologyVersion": 2, "replacedBy": "syn://type/geographic-geodetic"}\'::jsonb\n    WHERE id=\'syn://type/geographic-cartesian\'',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max)\n    VALUES (\'syn://type/geographic-geodetic\',\'latitude\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max)\n    VALUES (\'syn://type/geographic-geodetic\',\'longitude\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max)\n    VALUES (\'syn://type/geographic-geodetic\',\'altitude\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max)\n    VALUES (\'syn://type/geographic-geodetic\',\'errorMeters\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max)\n    VALUES (\'syn://type/geographic-geodetic\',\'altitudeDatum\',\'syn://type/position-text\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max)\n    VALUES (\'syn://type/geographic-geodetic\',\'verticalError\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'DROP TRIGGER coordinate_spaces_immutable ON "coordinateSpaces"',
    'UPDATE things t SET knowledge=jsonb_set(t.knowledge,\'{typeRef}\',\'"syn://type/geographic-geodetic"\'::jsonb,false)\n    WHERE t.id::text=\'syn://thing/space-wgs84-geodetic\' AND EXISTS (\n      SELECT 1 FROM "coordinateSpaces" s WHERE s."spaceRef"=t.id::text AND s."typeRef"=\'syn://type/geographic-cartesian\'\n    )',
    'UPDATE "coordinateSpaces" SET "typeRef"=\'syn://type/geographic-geodetic\',\n    definition=jsonb_set(definition::jsonb,\'{typeRef}\',\'"syn://type/geographic-geodetic"\'::jsonb,false)::json\n    WHERE "spaceRef"=\'syn://thing/space-wgs84-geodetic\' AND "typeRef"=\'syn://type/geographic-cartesian\'',
    'CREATE TRIGGER coordinate_spaces_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON "coordinateSpaces"\n    FOR EACH STATEMENT EXECUTE FUNCTION prevent_syn_position_rewrite()',
 )


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError(
        "Geodetic position history is append-only. Restore a pre-upgrade backup "
        "or provide an explicitly audited forward migration; do not rewrite hashes."
    )
