"""Append-only UTC awareness on the existing node transaction ledger.

Frozen SQL: no runtime schema imports. Stop writers and back up before upgrading.
"""
from alembic import op

revision = "20261117_0018"
down_revision = "20261117_0017"
branch_labels = None
depends_on = None

STATEMENTS = (
    '\nCREATE TABLE "nodeClocks" (\n\t"nodeRef" VARCHAR(270) NOT NULL, \n\t"sessionId" VARCHAR(36) NOT NULL, \n\t"emberRef" VARCHAR(270) NOT NULL, \n\tstate TEXT NOT NULL, \n\t"stateHash" VARCHAR(64) NOT NULL, \n\trevision BIGINT NOT NULL, \n\tPRIMARY KEY ("nodeRef"), \n\tCONSTRAINT "nodeClocksRevisionCheck" CHECK (revision > 0), \n\tFOREIGN KEY("nodeRef") REFERENCES "positionNodes" ("nodeRef"), \n\tFOREIGN KEY("emberRef") REFERENCES embers (id)\n)\n\n',
    '\nCREATE TABLE "clockRecords" (\n\t"objectRef" VARCHAR(270) NOT NULL, \n\t"nodeRef" VARCHAR(270) NOT NULL, \n\tkind VARCHAR(8) NOT NULL, \n\tsnapshot TEXT NOT NULL, \n\t"contentHash" VARCHAR(64) NOT NULL, \n\t"receiptSequence" BIGINT NOT NULL, \n\t"receiptHash" VARCHAR(64) NOT NULL, \n\tPRIMARY KEY ("objectRef"), \n\tCONSTRAINT "clockRecordsKindCheck" CHECK (kind IN (\'event\',\'ember\')), \n\tFOREIGN KEY("nodeRef") REFERENCES "positionNodes" ("nodeRef")\n)\n\n',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/clock-estimate' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/clock-estimate\',\'clock-estimate\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "UTC minus raw system time; precision and error may be unknown."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/clock-source-statistics' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/clock-source-statistics\',\'clock-source-statistics\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Measured offset drift and detrended jitter for one clock source."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/clock-reading\',\'clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "An uncorrected host/reference clock observation, including invalid readings."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/clock-session-started' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/clock-session-started\',\'clock-session-started\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "A new runtime starts with zero UTC correction and unknown accuracy."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/clock-discontinuity' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/clock-discontinuity\',\'clock-discontinuity\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "A host wall-clock step or suspend invalidated prior offset measurements."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/chrony-clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/chrony-clock-reading\',\'chrony-clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Clock observation from chrony."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/external-clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/external-clock-reading\',\'external-clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Clock observation from external."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/gps-clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/gps-clock-reading\',\'gps-clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Clock observation from gps."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/gpsd-pps-clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/gpsd-pps-clock-reading\',\'gpsd-pps-clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Clock observation from gpsd-pps."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/ntp-clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/ntp-clock-reading\',\'ntp-clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Clock observation from ntp."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/pps-clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/pps-clock-reading\',\'pps-clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Clock observation from pps."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/ptp-clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/ptp-clock-reading\',\'ptp-clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Clock observation from ptp."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/rtc-clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/rtc-clock-reading\',\'rtc-clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Clock observation from rtc."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM types WHERE id='syn://type/system-clock-reading' AND knowledge->>'ontology' IS DISTINCT FROM 'syn-clock') THEN RAISE EXCEPTION 'Clock Type identifier already in use'; END IF; END $$",
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/system-clock-reading\',\'system-clock-reading\',\'{"ontology": "syn-clock", "ontologyVersion": 1, "definition": "Clock observation from system."}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/clock-estimate\',\'syn://type/ember\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/clock-source-statistics\',\'syn://type/ember\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/clock-reading\',\'syn://type/event\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/clock-session-started\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/clock-discontinuity\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/chrony-clock-reading\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/external-clock-reading\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/gps-clock-reading\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/gpsd-pps-clock-reading\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/ntp-clock-reading\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/pps-clock-reading\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/ptp-clock-reading\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/rtc-clock-reading\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/system-clock-reading\',\'syn://type/clock-reading\') ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-estimate\',\'utcOffsetMs\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-estimate\',\'precisionMs\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-estimate\',\'uncertaintyMs\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-estimate\',\'driftPpm\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-estimate\',\'jitterMs\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-estimate\',\'status\',\'syn://type/position-text\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-estimate\',\'source\',\'syn://type/position-text\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-estimate\',\'stateHash\',\'syn://type/position-text\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-reading\',\'source\',\'syn://type/position-text\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-reading\',\'offsetMs\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-reading\',\'uncertaintyMs\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-reading\',\'resolutionMs\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-reading\',\'observedAt\',\'syn://type/position-timecode\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-reading\',\'metadata\',\'syn://type/position-mapping\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-source-statistics\',\'source\',\'syn://type/position-text\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-source-statistics\',\'jitterMs\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-source-statistics\',\'driftPpm\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-source-statistics\',\'samples\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES (\'syn://type/clock-source-statistics\',\'failures\',\'syn://type/position-number\',0,1) ON CONFLICT DO NOTHING',
    'CREATE TRIGGER clock_records_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON "clockRecords"\nFOR EACH STATEMENT EXECUTE FUNCTION prevent_syn_position_rewrite()',
    'CREATE TRIGGER clock_canonical_no_delete BEFORE DELETE OR TRUNCATE ON "nodeClocks"\nFOR EACH STATEMENT EXECUTE FUNCTION prevent_syn_position_rewrite()',
    'CREATE FUNCTION protect_syn_clock_primitive() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n IF EXISTS (SELECT 1 FROM "clockRecords" WHERE "objectRef"=OLD.id::text) THEN\n   RAISE EXCEPTION \'A ledger-recorded clock primitive cannot be changed\';\n END IF;\n IF TG_OP=\'DELETE\' THEN RETURN OLD; END IF;\n RETURN NEW;\nEND $$',
    'CREATE TRIGGER clock_ember_immutable BEFORE UPDATE OR DELETE ON embers\nFOR EACH ROW EXECUTE FUNCTION protect_syn_clock_primitive()',
    'CREATE TRIGGER clock_event_immutable BEFORE UPDATE OR DELETE ON events\nFOR EACH ROW EXECUTE FUNCTION protect_syn_clock_primitive()',
    'CREATE FUNCTION check_syn_clock_record() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n IF NEW."contentHash" <> encode(sha256(convert_to(NEW.snapshot,\'UTF8\')),\'hex\') THEN\n   RAISE EXCEPTION \'Clock snapshot digest differs\';\n END IF;\n IF NOT EXISTS (\n  SELECT 1 FROM syn_communication_messages m JOIN "positionNodes" n ON m.workspace_id=n."ledgerWorkspace"\n  WHERE n."nodeRef"=NEW."nodeRef" AND m.sequence=NEW."receiptSequence" AND m.hash=NEW."receiptHash"\n   AND m.payload->>\'profile\'=\'syn.clock-object/1\' AND m.payload->>\'objectRef\'=NEW."objectRef"\n   AND m.payload->>\'contentHash\'=NEW."contentHash" AND (m.payload->\'snapshot\')::jsonb=NEW.snapshot::jsonb\n ) THEN RAISE EXCEPTION \'Clock record requires its matching node ledger receipt\'; END IF;\n RETURN NEW;\nEND $$',
    'CREATE CONSTRAINT TRIGGER clock_record_receipt AFTER INSERT ON "clockRecords"\nDEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_syn_clock_record()',
    'CREATE FUNCTION check_syn_canonical_clock() RETURNS trigger LANGUAGE plpgsql AS $$\nDECLARE current_row "nodeClocks"; latest json;\nBEGIN\n SELECT * INTO current_row FROM "nodeClocks" WHERE "nodeRef"=NEW."nodeRef";\n IF current_row."stateHash" <> encode(sha256(convert_to(current_row.state,\'UTF8\')),\'hex\') THEN\n   RAISE EXCEPTION \'Clock state hash differs\';\n END IF;\n IF NOT EXISTS (SELECT 1 FROM "clockRecords" r WHERE r."objectRef"=current_row."emberRef"\n     AND r.kind=\'ember\' AND r."nodeRef"=current_row."nodeRef"\n     AND r.snapshot::jsonb->>\'typeRef\'=\'syn://type/clock-estimate\'\n     AND r.snapshot::jsonb->\'knowledge\'->>\'stateHash\'=current_row."stateHash"\n     AND r.snapshot::jsonb->\'knowledge\'->>\'sessionId\'=current_row."sessionId") THEN\n   RAISE EXCEPTION \'Canonical clock must reference its node/session estimate Ember\';\n END IF;\n SELECT m.payload INTO latest FROM syn_communication_messages m JOIN "positionNodes" n\n   ON m.workspace_id=n."ledgerWorkspace"\n   WHERE n."nodeRef"=current_row."nodeRef" AND m.payload->>\'profile\'=\'syn.clock-canonical/1\'\n   ORDER BY m.sequence DESC LIMIT 1;\n IF latest IS NULL OR latest->>\'emberRef\' IS DISTINCT FROM current_row."emberRef"\n   OR latest->>\'sessionId\' IS DISTINCT FROM current_row."sessionId"\n   OR latest->>\'stateHash\' IS DISTINCT FROM current_row."stateHash"\n   OR (latest->>\'revision\')::bigint IS DISTINCT FROM current_row.revision THEN\n   RAISE EXCEPTION \'Canonical clock has no matching ledger update\';\n END IF;\n RETURN NEW;\nEND $$',
    'CREATE CONSTRAINT TRIGGER canonical_clock_receipt AFTER INSERT OR UPDATE ON "nodeClocks"\nDEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_syn_canonical_clock()',
    'CREATE FUNCTION protect_syn_clock_target() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n IF EXISTS (SELECT 1 FROM "clockRecords" WHERE "objectRef"=OLD."eventRef"::text) THEN\n   RAISE EXCEPTION \'A clock Event target cannot be changed\';\n END IF;\n IF TG_OP=\'DELETE\' THEN RETURN OLD; END IF;\n RETURN NEW;\nEND $$',
    'CREATE TRIGGER clock_target_immutable BEFORE UPDATE OR DELETE ON "eventTargets"\nFOR EACH ROW EXECUTE FUNCTION protect_syn_clock_target()',
)


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Clock history is append-only; restore a backup or use an audited forward migration")
