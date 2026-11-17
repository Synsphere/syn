"""Node positions, immutable observations and existing-scheduler recurrence.

The SQL is frozen here, not imported from evolving runtime metadata. Downgrade
requires deliberate audit archival: this revision never silently erases history.
"""
from alembic import op

revision = "20261117_0016"
down_revision = "20261024_0015"
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE "positionNodes" (
	"nodeRef" VARCHAR(270) NOT NULL,
	"ledgerWorkspace" VARCHAR(36) NOT NULL,
	"createdAt" TIMESTAMP WITH TIME ZONE NOT NULL,
	"probeReport" JSON NOT NULL,
	PRIMARY KEY ("nodeRef"),
	FOREIGN KEY("nodeRef") REFERENCES things (id),
	UNIQUE ("ledgerWorkspace")
)""",
    """CREATE TABLE "coordinateSpaces" (
	"spaceRef" VARCHAR(270) NOT NULL,
	"typeRef" VARCHAR(270) NOT NULL,
	definition JSON NOT NULL,
	PRIMARY KEY ("spaceRef"),
	FOREIGN KEY("spaceRef") REFERENCES things (id),
	UNIQUE ("typeRef"),
	FOREIGN KEY("typeRef") REFERENCES types (id)
)""",
    """CREATE TABLE "nodePositions" (
	"nodeRef" VARCHAR(270) NOT NULL,
	"spaceRef" VARCHAR(270) NOT NULL,
	"emberRef" VARCHAR(270) NOT NULL,
	"observationEventRef" VARCHAR(270),
	"lastObservedAt" TIMESTAMP WITH TIME ZONE NOT NULL,
	revision BIGINT NOT NULL,
	PRIMARY KEY ("nodeRef", "spaceRef"),
	CONSTRAINT "nodePositionsRevisionCheck" CHECK (revision > 0),
	FOREIGN KEY("nodeRef") REFERENCES "positionNodes" ("nodeRef"),
	FOREIGN KEY("spaceRef") REFERENCES "coordinateSpaces" ("spaceRef"),
	FOREIGN KEY("emberRef") REFERENCES embers (id),
	FOREIGN KEY("observationEventRef") REFERENCES events (id)
)""",
    """CREATE TABLE "positionRecords" (
	"objectRef" VARCHAR(270) NOT NULL,
	"nodeRef" VARCHAR(270) NOT NULL,
	kind VARCHAR(8) NOT NULL,
	snapshot TEXT NOT NULL,
	"contentHash" VARCHAR(64) NOT NULL,
	"receiptSequence" BIGINT NOT NULL,
	"receiptHash" VARCHAR(64) NOT NULL,
	"sampleKey" VARCHAR(64),
	PRIMARY KEY ("objectRef"),
	CONSTRAINT "positionRecordsSampleUnique" UNIQUE ("nodeRef", kind, "sampleKey"),
	CONSTRAINT "positionRecordsKindCheck" CHECK (kind IN ('event','ember')),
	FOREIGN KEY("nodeRef") REFERENCES "positionNodes" ("nodeRef")
)""",
    """CREATE TABLE "positionDevices" (
	"nodeRef" VARCHAR(270) NOT NULL,
	"deviceId" VARCHAR(128) NOT NULL,
	descriptor JSON NOT NULL,
	active BOOLEAN NOT NULL,
	"lastSeen" TIMESTAMP WITH TIME ZONE NOT NULL,
	"lastError" VARCHAR(512),
	PRIMARY KEY ("nodeRef", "deviceId"),
	FOREIGN KEY("nodeRef") REFERENCES "positionNodes" ("nodeRef")
)""",
    """CREATE TABLE "jobSchedules" (
	"scheduleKey" VARCHAR(128) NOT NULL,
	"schedulerKey" VARCHAR(128) NOT NULL,
	handler VARCHAR(128) NOT NULL,
	arguments JSON NOT NULL,
	"intervalSeconds" FLOAT NOT NULL,
	"nextRunAt" TIMESTAMP WITH TIME ZONE NOT NULL,
	enabled BOOLEAN NOT NULL,
	"currentJobId" UUID,
	failures BIGINT NOT NULL,
	PRIMARY KEY ("scheduleKey"),
	CONSTRAINT "jobScheduleIntervalCheck" CHECK ("intervalSeconds" >= 1 AND "intervalSeconds" <= 86400),
	CONSTRAINT "jobScheduleFailuresCheck" CHECK (failures >= 0),
	FOREIGN KEY("currentJobId") REFERENCES jobs (id)
)""",
    """CREATE INDEX "jobSchedulesDueIdx" ON "jobSchedules" ("schedulerKey", enabled, "nextRunAt")""",
    """CREATE TABLE "jobDispatch" (
	"jobId" UUID NOT NULL,
	"schedulerKey" VARCHAR(128) NOT NULL,
	"scheduleKey" VARCHAR(128),
	PRIMARY KEY ("jobId"),
	FOREIGN KEY("jobId") REFERENCES jobs (id),
	FOREIGN KEY("scheduleKey") REFERENCES "jobSchedules" ("scheduleKey")
)""",
    """CREATE INDEX "ix_jobDispatch_schedulerKey" ON "jobDispatch" ("schedulerKey")""",
    """INSERT INTO types (id,name,knowledge) VALUES ('syn://type/coordinate-space','coordinate-space','{"ontology": "syn-position", "ontologyVersion": 1, "definition": "A coordinate reference frame with declared axes and units."}'::jsonb) ON CONFLICT (id) DO NOTHING""",
    """INSERT INTO types (id,name,knowledge) VALUES ('syn://type/space-position-estimate','space-position-estimate','{"ontology": "syn-position", "ontologyVersion": 1, "definition": "A qualified position belief about a node in one coordinate space."}'::jsonb) ON CONFLICT (id) DO NOTHING""",
    """INSERT INTO types (id,name,knowledge) VALUES ('syn://type/geographic-cartesian','geographic-cartesian','{"ontology": "syn-position", "ontologyVersion": 1, "definition": "WGS84 geodetic latitude/longitude and altitude; historical name, not ECEF XYZ."}'::jsonb) ON CONFLICT (id) DO NOTHING""",
    """INSERT INTO types (id,name,knowledge) VALUES ('syn://type/gps-reading','gps-reading','{"ontology": "syn-position", "ontologyVersion": 1, "definition": "A satellite receiver observation; coordinates may be unknown without a fix."}'::jsonb) ON CONFLICT (id) DO NOTHING""",
    """INSERT INTO types (id,name,knowledge) VALUES ('syn://type/os-location-reading','os-location-reading','{"ontology": "syn-position", "ontologyVersion": 1, "definition": "OS location observation, not necessarily a satellite fix."}'::jsonb) ON CONFLICT (id) DO NOTHING""",
    """INSERT INTO types (id,name,knowledge) VALUES ('syn://type/position-number','position-number','{"ontology": "syn-position", "ontologyVersion": 1, "definition": "Finite numeric position value; its unit belongs to the coordinate space."}'::jsonb) ON CONFLICT (id) DO NOTHING""",
    """INSERT INTO types (id,name,knowledge) VALUES ('syn://type/position-text','position-text','{"ontology": "syn-position", "ontologyVersion": 1, "definition": "Textual position provenance or device identifier."}'::jsonb) ON CONFLICT (id) DO NOTHING""",
    """INSERT INTO types (id,name,knowledge) VALUES ('syn://type/position-mapping','position-mapping','{"ontology": "syn-position", "ontologyVersion": 1, "definition": "A bounded mapping of coordinate or provenance fields."}'::jsonb) ON CONFLICT (id) DO NOTHING""",
    """INSERT INTO types (id,name,knowledge) VALUES ('syn://type/position-timecode','position-timecode','{"ontology": "syn-position", "ontologyVersion": 1, "definition": "UTC observation epoch with a specified offset."}'::jsonb) ON CONFLICT (id) DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/coordinate-space','syn://type/abstract-thing') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/space-position-estimate','syn://type/ember') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/geographic-cartesian','syn://type/space-position-estimate') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/gps-reading','syn://type/geographic-cartesian') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/gps-reading','syn://type/event') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/os-location-reading','syn://type/geographic-cartesian') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/os-location-reading','syn://type/event') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/position-number','syn://type/abstract-thing') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/position-text','syn://type/information-object') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/position-mapping','syn://type/information-object') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeParents" ("childRef","parentRef") VALUES ('syn://type/position-timecode','syn://type/temporal-thing') ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/space-position-estimate','spaceRef','syn://type/coordinate-space',1,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/space-position-estimate','coordinates','syn://type/position-mapping',1,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/space-position-estimate','error','syn://type/position-number',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/space-position-estimate','errorUnit','syn://type/position-text',1,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/space-position-estimate','observedAt','syn://type/position-timecode',1,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/space-position-estimate','uncertaintyMethod','syn://type/position-text',1,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/space-position-estimate','confidenceLevel','syn://type/position-number',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/space-position-estimate','source','syn://type/position-text',1,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/geographic-cartesian','latitude','syn://type/position-number',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/geographic-cartesian','longitude','syn://type/position-number',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/geographic-cartesian','altitude','syn://type/position-number',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/geographic-cartesian','errorMeters','syn://type/position-number',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/geographic-cartesian','altitudeDatum','syn://type/position-text',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/geographic-cartesian','verticalError','syn://type/position-number',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/gps-reading','deviceId','syn://type/position-text',1,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/gps-reading','model','syn://type/position-text',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/gps-reading','serialNumber','syn://type/position-text',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/gps-reading','satellitesLocked','syn://type/position-number',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/gps-reading','satellitesVisible','syn://type/position-number',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/gps-reading','timecode','syn://type/position-timecode',0,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/gps-reading','fix','syn://type/position-text',1,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/gps-reading','receivedAt','syn://type/position-timecode',1,1) ON CONFLICT DO NOTHING""",
    """INSERT INTO "typeProperties" ("type",name,"propertyTypeRef",min,max) VALUES ('syn://type/gps-reading','telemetry','syn://type/position-mapping',1,1) ON CONFLICT DO NOTHING""",
)


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    for statement in INTEGRITY:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Position history is append-only. Restore a pre-upgrade backup or explicitly archive it before downgrading.")


INTEGRITY = (
    '''CREATE FUNCTION prevent_syn_position_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN RAISE EXCEPTION 'Syn position history and coordinate frames are immutable'; END $$''',
    '''CREATE TRIGGER position_records_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON "positionRecords"
       FOR EACH STATEMENT EXECUTE FUNCTION prevent_syn_position_rewrite()''',
    '''CREATE TRIGGER coordinate_spaces_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON "coordinateSpaces"
       FOR EACH STATEMENT EXECUTE FUNCTION prevent_syn_position_rewrite()''',
    '''CREATE FUNCTION protect_syn_position_primitive() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
         IF EXISTS (SELECT 1 FROM "positionRecords" WHERE "objectRef"=OLD.id::text) THEN
           RAISE EXCEPTION 'A ledger-recorded position primitive cannot be changed';
         END IF;
         IF TG_OP='DELETE' THEN RETURN OLD; END IF;
         RETURN NEW;
       END $$''',
    '''CREATE TRIGGER position_ember_immutable BEFORE UPDATE OR DELETE ON embers
       FOR EACH ROW EXECUTE FUNCTION protect_syn_position_primitive()''',
    '''CREATE TRIGGER position_event_immutable BEFORE UPDATE OR DELETE ON events
       FOR EACH ROW EXECUTE FUNCTION protect_syn_position_primitive()''',
    '''CREATE FUNCTION check_syn_position_record() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
         IF NEW."contentHash" <> encode(sha256(convert_to(NEW.snapshot,'UTF8')),'hex') THEN
           RAISE EXCEPTION 'Position snapshot digest differs';
         END IF;
         IF NOT EXISTS (
           SELECT 1 FROM syn_communication_messages m JOIN "positionNodes" n
             ON m.workspace_id=n."ledgerWorkspace"
           WHERE n."nodeRef"=NEW."nodeRef" AND m.sequence=NEW."receiptSequence"
             AND m.hash=NEW."receiptHash" AND m.payload->>'profile'='syn.position-object/1'
             AND m.payload->>'objectRef'=NEW."objectRef"
             AND m.payload->>'contentHash'=NEW."contentHash"
             AND (m.payload->'snapshot')::jsonb=NEW.snapshot::jsonb
         ) THEN RAISE EXCEPTION 'Position record requires its matching existing-ledger receipt'; END IF;
         RETURN NEW;
       END $$''',
    '''CREATE CONSTRAINT TRIGGER position_record_receipt AFTER INSERT ON "positionRecords"
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_syn_position_record()''',
    '''CREATE FUNCTION check_syn_canonical_position() RETURNS trigger LANGUAGE plpgsql AS $$
       DECLARE current_row "nodePositions"; latest json; confirmation json;
       BEGIN
         SELECT * INTO current_row FROM "nodePositions"
           WHERE "nodeRef"=NEW."nodeRef" AND "spaceRef"=NEW."spaceRef";
         IF NOT EXISTS (SELECT 1 FROM "positionRecords" r WHERE r."objectRef"=current_row."emberRef"
             AND r.kind='ember' AND r."nodeRef"=current_row."nodeRef"
             AND r.snapshot::jsonb->'knowledge'->>'spaceRef'=current_row."spaceRef") THEN
           RAISE EXCEPTION 'Canonical position must reference an Ember in the same node and space';
         END IF;
         SELECT m.payload INTO latest FROM syn_communication_messages m JOIN "positionNodes" n
           ON m.workspace_id=n."ledgerWorkspace"
           WHERE n."nodeRef"=current_row."nodeRef" AND m.payload->>'spaceRef'=current_row."spaceRef"
             AND m.payload->>'profile'='syn.position-canonical/1'
           ORDER BY m.sequence DESC LIMIT 1;
         IF latest IS NULL OR latest->>'emberRef' IS DISTINCT FROM current_row."emberRef"
             OR (latest->>'revision')::bigint IS DISTINCT FROM current_row.revision
             OR latest->>'observationEventRef' IS DISTINCT FROM current_row."observationEventRef" THEN
           RAISE EXCEPTION 'Canonical pointer has no matching ledger update';
         END IF;
         SELECT m.payload INTO confirmation FROM syn_communication_messages m JOIN "positionNodes" n
           ON m.workspace_id=n."ledgerWorkspace"
           WHERE n."nodeRef"=current_row."nodeRef" AND m.payload->>'spaceRef'=current_row."spaceRef"
             AND m.payload->>'profile' IN ('syn.position-canonical/1','syn.position-confirmation/1')
           ORDER BY m.sequence DESC LIMIT 1;
         IF (confirmation->>'lastObservedAt')::timestamptz IS DISTINCT FROM current_row."lastObservedAt" THEN
           RAISE EXCEPTION 'Canonical freshness has no matching ledger receipt';
         END IF;
         RETURN NEW;
       END $$''',
    '''CREATE CONSTRAINT TRIGGER canonical_position_receipt AFTER INSERT OR UPDATE ON "nodePositions"
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_syn_canonical_position()''',
    '''CREATE TRIGGER canonical_position_no_delete BEFORE DELETE OR TRUNCATE ON "nodePositions"
       FOR EACH STATEMENT EXECUTE FUNCTION prevent_syn_position_rewrite()''',
)
