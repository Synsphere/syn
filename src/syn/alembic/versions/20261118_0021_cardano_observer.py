"""Trusted Cardano observations, independent peer cursors and dual native ledgers.

Schema and ontology statements are frozen in this migration. Runtime metadata
must not be imported when applying an historical database revision.
"""
from alembic import op

revision = "20261118_0021"
down_revision = "20261117_0020"
branch_labels = None
depends_on = None

STATEMENTS = (
    'CREATE TABLE "cardanoFollowers" (\n\tid VARCHAR(64) NOT NULL, \n\t"nodeRef" VARCHAR(270) NOT NULL, \n\t"networkMagic" BIGINT NOT NULL, \n\tpeer VARCHAR(512) NOT NULL, \n\tinitialized BOOLEAN NOT NULL, \n\tpoint JSON, \n\tanchor JSON, \n\trevision BIGINT NOT NULL, \n\t"lastObservedAt" TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT "cardanoFollowerSourceUnique" UNIQUE ("nodeRef", "networkMagic", peer), \n\tCONSTRAINT "cardanoFollowerBounds" CHECK ("networkMagic" BETWEEN 0 AND 4294967295 AND revision >= 0), \n\tFOREIGN KEY("nodeRef") REFERENCES "positionNodes" ("nodeRef")\n)',
    'CREATE TABLE "cardanoBlocks" (\n\thash VARCHAR(64) NOT NULL, \n\t"eventRef" VARCHAR(270) NOT NULL, \n\t"eventWorkspace" VARCHAR(36) NOT NULL, \n\tslot BIGINT NOT NULL, \n\t"blockNumber" BIGINT NOT NULL, \n\t"previousHash" VARCHAR(64), \n\tera VARCHAR(32) NOT NULL, \n\t"headerCbor" BYTEA NOT NULL, \n\tsnapshot TEXT NOT NULL, \n\t"contentHash" VARCHAR(64) NOT NULL, \n\tPRIMARY KEY (hash), \n\tCONSTRAINT "cardanoBlockBounds" CHECK (slot >= 0 AND "blockNumber" >= 0), \n\tUNIQUE ("eventRef"), \n\tFOREIGN KEY("eventRef") REFERENCES events (id), \n\tUNIQUE ("eventWorkspace")\n)',
    'CREATE TABLE "cardanoPeerChain" (\n\t"followerId" VARCHAR(64) NOT NULL, \n\t"blockHash" VARCHAR(64) NOT NULL, \n\tslot BIGINT NOT NULL, \n\t"blockNumber" BIGINT NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\t"observationRef" VARCHAR(270) NOT NULL, \n\tPRIMARY KEY ("followerId", "blockHash"), \n\tFOREIGN KEY("followerId") REFERENCES "cardanoFollowers" (id), \n\tFOREIGN KEY("blockHash") REFERENCES "cardanoBlocks" (hash), \n\tFOREIGN KEY("observationRef") REFERENCES embers (id)\n)',
    'CREATE TABLE "cardanoObservations" (\n\t"emberRef" VARCHAR(270) NOT NULL, \n\t"followerId" VARCHAR(64) NOT NULL, \n\t"eventRef" VARCHAR(270) NOT NULL, \n\trevision BIGINT NOT NULL, \n\taction VARCHAR(16) NOT NULL, \n\tsnapshot TEXT NOT NULL, \n\t"contentHash" VARCHAR(64) NOT NULL, \n\t"nodeReceiptSequence" BIGINT NOT NULL, \n\t"nodeReceiptHash" VARCHAR(64) NOT NULL, \n\t"eventReceiptSequence" BIGINT NOT NULL, \n\t"eventReceiptHash" VARCHAR(64) NOT NULL, \n\tPRIMARY KEY ("emberRef"), \n\tCONSTRAINT "cardanoObservationTransitionUnique" UNIQUE ("followerId", revision, "eventRef", action), \n\tCONSTRAINT "cardanoObservationKind" CHECK (action IN (\'apply\',\'rollback\') AND revision > 0), \n\tFOREIGN KEY("emberRef") REFERENCES embers (id), \n\tFOREIGN KEY("followerId") REFERENCES "cardanoFollowers" (id), \n\tFOREIGN KEY("eventRef") REFERENCES events (id)\n)',
    'CREATE INDEX "cardanoPeerChainActiveIdx" ON "cardanoPeerChain" ("followerId", active, slot)',
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/blockchain\',\'blockchain\',\'{"definition":"A distributed append-oriented chain with an independently defined consensus protocol.","ontology":"syn-cardano","ontologyVersion": 1}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/blockchain\',\'syn://type/system\') ON CONFLICT DO NOTHING',
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/blockchain-block\',\'blockchain-block\',\'{"definition":"A block produced on a blockchain; observation does not establish finality.","ontology":"syn-cardano","ontologyVersion": 1}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/blockchain-block\',\'syn://type/event\') ON CONFLICT DO NOTHING',
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/cardano-block\',\'cardano-block\',\'{"definition":"A Cardano block identified by its Cardano header hash.","ontology":"syn-cardano","ontologyVersion": 1}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/cardano-block\',\'syn://type/blockchain-block\') ON CONFLICT DO NOTHING',
    'INSERT INTO types (id,name,knowledge) VALUES (\'syn://type/cardano-block-observation\',\'cardano-block-observation\',\'{"definition":"An observer\'\'s source-qualified observation or retraction of a Cardano block.","ontology":"syn-cardano","ontologyVersion": 1}\'::jsonb) ON CONFLICT (id) DO NOTHING',
    'INSERT INTO "typeParents" ("childRef","parentRef") VALUES (\'syn://type/cardano-block-observation\',\'syn://type/observation-result\') ON CONFLICT DO NOTHING',
    'INSERT INTO things (id,name,"typeRef",knowledge) VALUES (\'syn://thing/cardano\',\'Cardano\',\'syn://type/blockchain\',\'{"profile":"syn.blockchain/1","family":"cardano"}\'::jsonb) ON CONFLICT (id) DO NOTHING',
)


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    for statement in INTEGRITY:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Cardano evidence is append-only; archive or restore explicitly before downgrading")


INTEGRITY = (
    '''CREATE FUNCTION syn_cardano_no_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN RAISE EXCEPTION 'Cardano observation history cannot be rewritten'; END $$''',
    '''CREATE TRIGGER cardano_blocks_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
       ON "cardanoBlocks" FOR EACH STATEMENT EXECUTE FUNCTION syn_cardano_no_rewrite()''',
    '''CREATE TRIGGER cardano_observations_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
       ON "cardanoObservations" FOR EACH STATEMENT EXECUTE FUNCTION syn_cardano_no_rewrite()''',
    '''CREATE TRIGGER cardano_followers_no_delete BEFORE DELETE OR TRUNCATE
       ON "cardanoFollowers" FOR EACH STATEMENT EXECUTE FUNCTION syn_cardano_no_rewrite()''',
    '''CREATE TRIGGER cardano_chain_no_delete BEFORE DELETE OR TRUNCATE
       ON "cardanoPeerChain" FOR EACH STATEMENT EXECUTE FUNCTION syn_cardano_no_rewrite()''',
    '''CREATE FUNCTION syn_cardano_source_identity() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
         IF NEW.id <> OLD.id OR NEW."nodeRef" <> OLD."nodeRef"
            OR NEW."networkMagic" <> OLD."networkMagic" OR NEW.peer <> OLD.peer THEN
           RAISE EXCEPTION 'Cardano source identity cannot change in place';
         END IF;
         RETURN NEW;
       END $$''',
    '''CREATE TRIGGER cardano_source_identity BEFORE UPDATE ON "cardanoFollowers"
       FOR EACH ROW EXECUTE FUNCTION syn_cardano_source_identity()''',
    '''CREATE FUNCTION syn_cardano_protect_primitive() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
         IF EXISTS (SELECT 1 FROM "cardanoBlocks" WHERE "eventRef"=OLD.id::text)
            OR EXISTS (SELECT 1 FROM "cardanoObservations" WHERE "emberRef"=OLD.id::text) THEN
           RAISE EXCEPTION 'A ledger-recorded Cardano Event or Ember is immutable';
         END IF;
         IF TG_OP='DELETE' THEN RETURN OLD; END IF;
         RETURN NEW;
       END $$''',
    '''CREATE TRIGGER cardano_event_immutable BEFORE UPDATE OR DELETE ON events
       FOR EACH ROW EXECUTE FUNCTION syn_cardano_protect_primitive()''',
    '''CREATE TRIGGER cardano_ember_immutable BEFORE UPDATE OR DELETE ON embers
       FOR EACH ROW EXECUTE FUNCTION syn_cardano_protect_primitive()''',
    '''CREATE FUNCTION syn_cardano_protect_chronicle() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
         IF TG_OP='TRUNCATE' THEN
           IF EXISTS (SELECT 1 FROM "objectChronicle" WHERE details->>'profile'='syn.cardano-ledger-entry/1') THEN
             RAISE EXCEPTION 'Cardano native ledger entries cannot be truncated';
           END IF;
           RETURN NULL;
         END IF;
         IF OLD.details->>'profile'='syn.cardano-ledger-entry/1' THEN
           RAISE EXCEPTION 'Cardano native ledger entries are immutable';
         END IF;
         IF TG_OP='DELETE' THEN RETURN OLD; END IF;
         RETURN NEW;
       END $$''',
    '''CREATE TRIGGER cardano_chronicle_immutable BEFORE UPDATE OR DELETE ON "objectChronicle"
       FOR EACH ROW EXECUTE FUNCTION syn_cardano_protect_chronicle()''',
    '''CREATE TRIGGER cardano_chronicle_no_truncate BEFORE TRUNCATE ON "objectChronicle"
       FOR EACH STATEMENT EXECUTE FUNCTION syn_cardano_protect_chronicle()''',
    '''CREATE FUNCTION syn_cardano_check_block() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
         IF NEW."eventRef" <> 'syn://event/' || NEW.hash
            OR NEW."contentHash" <> encode(sha256(convert_to(NEW.snapshot,'UTF8')),'hex')
            OR NEW.snapshot::jsonb->>'id' IS DISTINCT FROM NEW."eventRef"
            OR NEW.snapshot::jsonb->'knowledge'->>'blockHash' IS DISTINCT FROM NEW.hash
            OR NOT EXISTS (SELECT 1 FROM events e WHERE e.id::text=NEW."eventRef"
                AND e."typeRef"::text='syn://type/cardano-block' AND e."occurredAt" IS NULL
                AND e.name=NEW.snapshot::jsonb->>'name'
                AND e.knowledge=NEW.snapshot::jsonb->'knowledge') THEN
           RAISE EXCEPTION 'Cardano block snapshot differs from its native Event';
         END IF;
         RETURN NEW;
       END $$''',
    '''CREATE CONSTRAINT TRIGGER cardano_block_snapshot AFTER INSERT ON "cardanoBlocks"
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION syn_cardano_check_block()''',
    '''CREATE FUNCTION syn_cardano_check_observation() RETURNS trigger LANGUAGE plpgsql AS $$
       DECLARE source_row "cardanoFollowers"; block_row "cardanoBlocks";
               node_message syn_communication_messages; event_message syn_communication_messages;
               node_workspace text;
       BEGIN
         SELECT * INTO source_row FROM "cardanoFollowers" WHERE id=NEW."followerId";
         SELECT * INTO block_row FROM "cardanoBlocks" WHERE "eventRef"=NEW."eventRef";
         SELECT "ledgerWorkspace" INTO node_workspace FROM "positionNodes" WHERE "nodeRef"=source_row."nodeRef";
         SELECT * INTO node_message FROM syn_communication_messages
           WHERE workspace_id=node_workspace AND sequence=NEW."nodeReceiptSequence";
         SELECT * INTO event_message FROM syn_communication_messages
           WHERE workspace_id=block_row."eventWorkspace" AND sequence=NEW."eventReceiptSequence";
         IF NEW."contentHash" <> encode(sha256(convert_to(NEW.snapshot,'UTF8')),'hex')
            OR NEW.snapshot::jsonb->>'id' IS DISTINCT FROM NEW."emberRef"
            OR NEW.snapshot::jsonb->'knowledge'->>'action' IS DISTINCT FROM NEW.action
            OR NEW.snapshot::jsonb->'knowledge'->>'eventRef' IS DISTINCT FROM NEW."eventRef"
            OR NEW.snapshot::jsonb->'knowledge'->>'nodeRef' IS DISTINCT FROM source_row."nodeRef"
            OR NEW.snapshot::jsonb->'knowledge'->>'peer' IS DISTINCT FROM source_row.peer
            OR (NEW.snapshot::jsonb->'knowledge'->>'revision')::bigint IS DISTINCT FROM NEW.revision
            OR (NEW.snapshot::jsonb->'knowledge'->>'networkMagic')::bigint IS DISTINCT FROM source_row."networkMagic"
            OR node_message.hash IS DISTINCT FROM NEW."nodeReceiptHash"
            OR event_message.hash IS DISTINCT FROM NEW."eventReceiptHash"
            OR node_message.payload::jsonb IS DISTINCT FROM event_message.payload::jsonb
            OR node_message.payload->>'profile' IS DISTINCT FROM 'syn.cardano-observation/1'
            OR node_message.payload->>'emberRef' IS DISTINCT FROM NEW."emberRef"
            OR node_message.payload->>'eventRef' IS DISTINCT FROM NEW."eventRef"
            OR node_message.payload->>'contentHash' IS DISTINCT FROM NEW."contentHash"
            OR (node_message.payload->'snapshot')::jsonb IS DISTINCT FROM NEW.snapshot::jsonb
            OR (node_message.payload->'eventSnapshot')::jsonb IS DISTINCT FROM block_row.snapshot::jsonb
            OR NOT EXISTS (SELECT 1 FROM embers e WHERE e.id::text=NEW."emberRef"
                AND e."typeRef"::text='syn://type/cardano-block-observation'
                AND e.name=NEW.snapshot::jsonb->>'name'
                AND e.knowledge=NEW.snapshot::jsonb->'knowledge') THEN
           RAISE EXCEPTION 'Cardano observation requires matching native objects and both ledger receipts';
         END IF;
         IF NOT EXISTS (SELECT 1 FROM "objectChronicle" c WHERE c."objectRef"::text=source_row."nodeRef"
             AND c."eventRef"::text=NEW."eventRef" AND c.kind='ledger'
             AND c.details->>'profile'='syn.cardano-ledger-entry/1'
             AND c.details->>'emberRef'=NEW."emberRef" AND c.details->>'receiptHash'=NEW."nodeReceiptHash")
            OR NOT EXISTS (SELECT 1 FROM "objectChronicle" c WHERE c."objectRef"::text=NEW."eventRef"
             AND c."eventRef"::text=NEW."eventRef" AND c.kind='ledger'
             AND c.details->>'profile'='syn.cardano-ledger-entry/1'
             AND c.details->>'emberRef'=NEW."emberRef" AND c.details->>'receiptHash'=NEW."eventReceiptHash") THEN
           RAISE EXCEPTION 'Cardano observation requires both native object ledger links';
         END IF;
         RETURN NEW;
       END $$''',
    '''CREATE CONSTRAINT TRIGGER cardano_observation_receipts AFTER INSERT ON "cardanoObservations"
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION syn_cardano_check_observation()''',
    '''CREATE FUNCTION syn_cardano_check_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$
       DECLARE source_row "cardanoFollowers"; latest json;
       BEGIN
         SELECT * INTO source_row FROM "cardanoFollowers" WHERE id=NEW.id;
         IF NOT source_row.initialized AND source_row.revision=0 THEN RETURN NEW; END IF;
         SELECT m.payload INTO latest FROM syn_communication_messages m JOIN "positionNodes" n
           ON m.workspace_id=n."ledgerWorkspace" WHERE n."nodeRef"=source_row."nodeRef"
           AND m.payload->>'profile'='syn.cardano-checkpoint/1'
           AND m.payload->>'peer'=source_row.peer
           AND (m.payload->>'networkMagic')::bigint=source_row."networkMagic"
           ORDER BY m.sequence DESC LIMIT 1;
         IF latest IS NULL OR NOT source_row.initialized
            OR (latest->>'revision')::bigint IS DISTINCT FROM source_row.revision
            OR (latest->'point')::jsonb IS DISTINCT FROM COALESCE(source_row.point::jsonb,'null'::jsonb) THEN
           RAISE EXCEPTION 'Cardano checkpoint requires its latest committed node ledger entry';
         END IF;
         RETURN NEW;
       END $$''',
    '''CREATE CONSTRAINT TRIGGER cardano_checkpoint_receipt AFTER INSERT OR UPDATE ON "cardanoFollowers"
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION syn_cardano_check_checkpoint()''',
    '''CREATE FUNCTION syn_cardano_check_chain() RETURNS trigger LANGUAGE plpgsql AS $$
       DECLARE current_row "cardanoPeerChain"; latest "cardanoObservations";
       BEGIN
         SELECT * INTO current_row FROM "cardanoPeerChain"
           WHERE "followerId"=NEW."followerId" AND "blockHash"=NEW."blockHash";
         SELECT o.* INTO latest FROM "cardanoObservations" o JOIN "cardanoBlocks" b ON o."eventRef"=b."eventRef"
           WHERE o."followerId"=current_row."followerId" AND b.hash=current_row."blockHash"
           ORDER BY o.revision DESC LIMIT 1;
         IF latest."emberRef" IS NULL
            OR NOT EXISTS (SELECT 1 FROM "cardanoBlocks" b WHERE b.hash=current_row."blockHash"
                AND b.slot=current_row.slot AND b."blockNumber"=current_row."blockNumber")
            OR (current_row.active AND (latest.action<>'apply' OR latest."emberRef"<>current_row."observationRef"))
            OR (NOT current_row.active AND (latest.action<>'rollback'
                OR latest.snapshot::jsonb->'knowledge'->>'retracts' IS DISTINCT FROM current_row."observationRef")) THEN
           RAISE EXCEPTION 'Cardano peer chain projection requires its latest observation or retraction';
         END IF;
         RETURN NEW;
       END $$''',
    '''CREATE CONSTRAINT TRIGGER cardano_peer_chain_receipt AFTER INSERT OR UPDATE ON "cardanoPeerChain"
       DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION syn_cardano_check_chain()''',
)
