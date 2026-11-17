"""Clock projections use native Syn Events/Embers and the existing node ledger."""
from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKey, JSON, String, Table, Text
from syn.positions.schema import metadata, insert_ignore, primitive, typeParents, typeProperties
from .types import KINDS

clocks = Table("nodeClocks", metadata,
    Column("nodeRef", String(270), ForeignKey("positionNodes.nodeRef"), primary_key=True),
    Column("sessionId", String(36), nullable=False),
    Column("emberRef", String(270), ForeignKey("embers.id"), nullable=False),
    Column("state", Text, nullable=False),
    Column("stateHash", String(64), nullable=False),
    Column("revision", BigInteger, nullable=False),
    CheckConstraint("revision > 0", name="nodeClocksRevisionCheck"))
records = Table("clockRecords", metadata,
    Column("objectRef", String(270), primary_key=True),
    Column("nodeRef", String(270), ForeignKey("positionNodes.nodeRef"), nullable=False),
    Column("kind", String(8), nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("contentHash", String(64), nullable=False),
    Column("receiptSequence", BigInteger, nullable=False),
    Column("receiptHash", String(64), nullable=False),
    CheckConstraint("kind IN ('event','ember')", name="clockRecordsKindCheck"))

TYPE_DEFINITIONS = (
    ("clock-estimate", ("ember",), "UTC minus raw system time; precision and error may be unknown."),
    ("clock-source-statistics", ("ember",), "Measured offset drift and detrended jitter for one clock source."),
    ("clock-reading", ("event",), "An uncorrected host/reference clock observation, including invalid readings."),
    ("clock-session-started", ("clock-reading",), "A new runtime starts with zero UTC correction and unknown accuracy."),
    ("clock-discontinuity", ("clock-reading",), "A host wall-clock step or suspend invalidated prior offset measurements."),
    *((kind+"-clock-reading", ("clock-reading",), "Clock observation from "+kind+".") for kind in sorted(KINDS)),
)
PROPERTIES = {
    "clock-estimate": {"utcOffsetMs": "position-number", "precisionMs": "position-number",
        "uncertaintyMs": "position-number", "driftPpm": "position-number", "jitterMs": "position-number",
        "status": "position-text", "source": "position-text", "stateHash": "position-text"},
    "clock-reading": {"source": "position-text", "offsetMs": "position-number",
        "uncertaintyMs": "position-number", "resolutionMs": "position-number",
        "observedAt": "position-timecode", "metadata": "position-mapping"},
    "clock-source-statistics": {"source": "position-text", "jitterMs": "position-number",
        "driftPpm": "position-number", "samples": "position-number", "failures": "position-number"},
}


def seed_types(session):
    for slug, _, definition in TYPE_DEFINITIONS:
        insert_ignore(session, primitive["types"], {"id": "syn://type/"+slug, "name": slug,
            "knowledge": {"ontology": "syn-clock", "ontologyVersion": 1, "definition": definition}}, ["id"])
    for slug, parents, _ in TYPE_DEFINITIONS:
        for parent in parents:
            insert_ignore(session, typeParents, {"childRef": "syn://type/"+slug,
                "parentRef": "syn://type/"+parent}, ["childRef", "parentRef"])
    for slug, props in PROPERTIES.items():
        for name, typ in props.items():
            insert_ignore(session, typeProperties, {"type": "syn://type/"+slug, "name": name,
                "propertyTypeRef": "syn://type/"+typ, "min": 0, "max": 1}, ["type", "name"])
