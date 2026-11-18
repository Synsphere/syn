"""Cardano projections; native objects and receipt chains remain Syn-owned."""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime,
                        ForeignKey, Identity, Index, Integer, JSON, LargeBinary, String,
                        Table, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import ENUM

from syn.positions.schema import (metadata, primitive, typeParents, eventTargets,
                                  nodes as position_nodes, insert_ignore)


chronicle = Table("objectChronicle", metadata,
    Column("entryId", Integer, Identity(always=True), primary_key=True),
    Column("objectRef", String, nullable=False),
    Column("eventRef", String),
    Column("kind", ENUM("ledger", "provenance", name="chronicleKind", create_type=False)
           .with_variant(String, "sqlite"), nullable=False),
    Column("begunAt", DateTime(timezone=True), nullable=False),
    Column("endedAt", DateTime(timezone=True)),
    Column("sequenceNumber", BigInteger, nullable=False),
    Column("details", JSON, nullable=False),
    UniqueConstraint("objectRef", "kind", "sequenceNumber"))

followers = Table("cardanoFollowers", metadata,
    Column("id", String(64), primary_key=True),
    Column("nodeRef", String(270), ForeignKey("positionNodes.nodeRef"), nullable=False),
    Column("networkMagic", BigInteger, nullable=False),
    Column("peer", String(512), nullable=False),
    Column("initialized", Boolean, nullable=False),
    Column("point", JSON), Column("anchor", JSON),
    Column("revision", BigInteger, nullable=False),
    Column("lastObservedAt", DateTime(timezone=True)),
    UniqueConstraint("nodeRef", "networkMagic", "peer", name="cardanoFollowerSourceUnique"),
    CheckConstraint('"networkMagic" BETWEEN 0 AND 4294967295 AND revision >= 0',
                    name="cardanoFollowerBounds"))

blocks = Table("cardanoBlocks", metadata,
    Column("hash", String(64), primary_key=True),
    Column("eventRef", String(270), ForeignKey("events.id"), nullable=False, unique=True),
    Column("eventWorkspace", String(36), nullable=False, unique=True),
    Column("slot", BigInteger, nullable=False),
    Column("blockNumber", BigInteger, nullable=False),
    Column("previousHash", String(64)),
    Column("era", String(32), nullable=False),
    Column("headerCbor", LargeBinary, nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("contentHash", String(64), nullable=False),
    CheckConstraint('slot >= 0 AND "blockNumber" >= 0', name="cardanoBlockBounds"))

chain = Table("cardanoPeerChain", metadata,
    Column("followerId", String(64), ForeignKey("cardanoFollowers.id"), primary_key=True),
    Column("blockHash", String(64), ForeignKey("cardanoBlocks.hash"), primary_key=True),
    Column("slot", BigInteger, nullable=False),
    Column("blockNumber", BigInteger, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("observationRef", String(270), ForeignKey("embers.id"), nullable=False))
Index("cardanoPeerChainActiveIdx", chain.c.followerId, chain.c.active, chain.c.slot)

records = Table("cardanoObservations", metadata,
    Column("emberRef", String(270), ForeignKey("embers.id"), primary_key=True),
    Column("followerId", String(64), ForeignKey("cardanoFollowers.id"), nullable=False),
    Column("eventRef", String(270), ForeignKey("events.id"), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("action", String(16), nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("contentHash", String(64), nullable=False),
    Column("nodeReceiptSequence", BigInteger, nullable=False),
    Column("nodeReceiptHash", String(64), nullable=False),
    Column("eventReceiptSequence", BigInteger, nullable=False),
    Column("eventReceiptHash", String(64), nullable=False),
    UniqueConstraint("followerId", "revision", "eventRef", "action",
                     name="cardanoObservationTransitionUnique"),
    CheckConstraint("action IN ('apply','rollback') AND revision > 0",
                    name="cardanoObservationKind"))

CARDANO_TABLES = (followers, blocks, chain, records)
TYPE_DEFINITIONS = (
    ("blockchain", "system", "A distributed append-oriented chain with an independently defined consensus protocol."),
    ("blockchain-block", "event", "A block produced on a blockchain; observation does not establish finality."),
    ("cardano-block", "blockchain-block", "A Cardano block identified by its Cardano header hash."),
    ("cardano-block-observation", "observation-result", "An observer's source-qualified observation or retraction of a Cardano block."),
)


def seed_types(session):
    """Used by isolated contract fixtures; production installation is migration-owned."""
    for slug, parent, definition in TYPE_DEFINITIONS:
        insert_ignore(session, primitive["types"], {
            "id": "syn://type/" + slug, "name": slug,
            "knowledge": {"ontology": "syn-cardano", "ontologyVersion": 1,
                          "definition": definition}}, ["id"])
        insert_ignore(session, typeParents, {"childRef": "syn://type/" + slug,
            "parentRef": "syn://type/" + parent}, ["childRef", "parentRef"])
    insert_ignore(session, primitive["things"], {
        "id": "syn://thing/cardano", "name": "Cardano", "typeRef": "syn://type/blockchain",
        "knowledge": {"profile": "syn.blockchain/1", "family": "cardano"}}, ["id"])
