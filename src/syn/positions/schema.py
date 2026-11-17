"""SQLAlchemy Core contracts for positions; primitive tables remain Syn-owned."""
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime,
                        ForeignKey, Integer, JSON, MetaData, String, Table, Text,
                        UniqueConstraint)

metadata = MetaData()
# Read/write projections of existing native primitives (NOT created by this module).
primitive = {}
for name in ("things", "embers", "events", "types"):
    cols = [Column("id", String, primary_key=True), Column("name", Text, nullable=False),
            Column("knowledge", JSON), Column("knowledgeUpdated", DateTime(timezone=True))]
    if name != "types":
        cols.append(Column("typeRef", String, nullable=False))
    if name == "events":
        cols.append(Column("occurredAt", DateTime(timezone=True)))
    primitive[name] = Table(name, metadata, *cols)
typeParents = Table("typeParents", metadata, Column("childRef", String, primary_key=True),
                    Column("parentRef", String, primary_key=True))
typeProperties = Table("typeProperties", metadata, Column("type", String, primary_key=True),
                       Column("name", String, primary_key=True), Column("propertyTypeRef", String),
                       Column("min", BigInteger), Column("max", BigInteger))
eventTargets = Table("eventTargets", metadata, Column("eventRef", String, primary_key=True),
                     Column("targetRef", String, primary_key=True), Column("role", String), Column("position", Integer))

nodes = Table("positionNodes", metadata,
    Column("nodeRef", String(270), ForeignKey("things.id"), primary_key=True),
    Column("ledgerWorkspace", String(36), nullable=False, unique=True),
    Column("createdAt", DateTime(timezone=True), nullable=False),
    Column("probeReport", JSON, nullable=False),
)
spaces = Table("coordinateSpaces", metadata,
    Column("spaceRef", String(270), ForeignKey("things.id"), primary_key=True),
    Column("typeRef", String(270), ForeignKey("types.id"), nullable=False, unique=True),
    Column("definition", JSON, nullable=False),
)
positions = Table("nodePositions", metadata,
    Column("nodeRef", String(270), ForeignKey("positionNodes.nodeRef"), primary_key=True),
    Column("spaceRef", String(270), ForeignKey("coordinateSpaces.spaceRef"), primary_key=True),
    Column("emberRef", String(270), ForeignKey("embers.id"), nullable=False),
    Column("observationEventRef", String(270), ForeignKey("events.id")),
    Column("lastObservedAt", DateTime(timezone=True), nullable=False),
    Column("revision", BigInteger, nullable=False),
    CheckConstraint('revision > 0', name="nodePositionsRevisionCheck"),
)
records = Table("positionRecords", metadata,
    Column("objectRef", String(270), primary_key=True),
    Column("nodeRef", String(270), ForeignKey("positionNodes.nodeRef"), nullable=False),
    Column("kind", String(8), nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("contentHash", String(64), nullable=False),
    Column("receiptSequence", BigInteger, nullable=False),
    Column("receiptHash", String(64), nullable=False),
    Column("sampleKey", String(64)),
    UniqueConstraint("nodeRef", "kind", "sampleKey", name="positionRecordsSampleUnique"),
    CheckConstraint("kind IN ('event','ember')", name="positionRecordsKindCheck"),
)
devices = Table("positionDevices", metadata,
    Column("nodeRef", String(270), ForeignKey("positionNodes.nodeRef"), primary_key=True),
    Column("deviceId", String(128), primary_key=True),
    Column("descriptor", JSON, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("lastSeen", DateTime(timezone=True), nullable=False),
    Column("lastError", String(512)),
)
POSITION_TABLES = (nodes, spaces, positions, records, devices)


def insert_ignore(session, table, values, keys):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    dialect = session.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise ValueError("Syn position persistence requires PostgreSQL")
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    return session.execute(insert(table).values(**values).on_conflict_do_nothing(index_elements=keys))


TYPE_DEFINITIONS = (
    ("coordinate-space", ("abstract-thing",), "A coordinate reference frame with declared axes and units."),
    ("space-position-estimate", ("ember",), "A qualified position belief about a node in one coordinate space."),
    ("geographic-geodetic", ("space-position-estimate",), "WGS84 geodetic latitude/longitude in decimal degrees and altitude in metres with an explicit altitude datum; not Cartesian XYZ."),
    ("geographic-cartesian", ("geographic-geodetic",), "Deprecated compatibility identifier for immutable historical geographic-geodetic records; do not use for new estimates."),
    ("gps-reading", ("geographic-geodetic", "event"), "A satellite receiver observation; coordinates may be unknown without a fix."),
    ("os-location-reading", ("geographic-geodetic", "event"), "OS location observation, not necessarily a satellite fix."),
    ("position-number", ("abstract-thing",), "Finite numeric position value; its unit belongs to the coordinate space."),
    ("position-text", ("information-object",), "Textual position provenance or device identifier."),
    ("position-mapping", ("information-object",), "A bounded mapping of coordinate or provenance fields."),
    ("position-timecode", ("temporal-thing",), "UTC observation epoch with a specified offset."),
)
PROPERTIES = {
    "space-position-estimate": {"spaceRef": ("coordinate-space", 1), "coordinates": ("position-mapping", 1),
        "error": ("position-number", 0), "errorUnit": ("position-text", 1),
        "observedAt": ("position-timecode", 1), "uncertaintyMethod": ("position-text", 1),
        "confidenceLevel": ("position-number", 0), "source": ("position-text", 1)},
    "geographic-geodetic": {"latitude": ("position-number", 0), "longitude": ("position-number", 0),
        "altitude": ("position-number", 0), "errorMeters": ("position-number", 0),
        "altitudeDatum": ("position-text", 0), "verticalError": ("position-number", 0)},
    "gps-reading": {"deviceId": ("position-text", 1), "model": ("position-text", 0),
        "serialNumber": ("position-text", 0), "satellitesLocked": ("position-number", 0),
        "satellitesVisible": ("position-number", 0), "timecode": ("position-timecode", 0),
        "fix": ("position-text", 1), "receivedAt": ("position-timecode", 1), "telemetry": ("position-mapping", 1)},
}


def seed_types(session):
    for slug, parents, definition in TYPE_DEFINITIONS:
        ref = "syn://type/"+slug
        knowledge = {"ontology": "syn-position", "ontologyVersion": 2, "definition": definition}
        if slug == "geographic-cartesian":
            knowledge.update(deprecated=True, replacedBy="syn://type/geographic-geodetic")
        insert_ignore(session, primitive["types"], {"id": ref,
            "name": "geographic-geodetic (legacy alias)" if slug == "geographic-cartesian" else slug,
            "knowledge": knowledge}, ["id"])
    for slug, parents, _ in TYPE_DEFINITIONS:
        for parent in parents:
            insert_ignore(session, typeParents, {"childRef": "syn://type/"+slug,
                          "parentRef": "syn://type/"+parent}, ["childRef", "parentRef"])
    for slug, props in PROPERTIES.items():
        for name, (value_type, minimum) in props.items():
            insert_ignore(session, typeProperties, {"type": "syn://type/"+slug, "name": name,
                "propertyTypeRef": "syn://type/"+value_type, "min": minimum, "max": 1}, ["type", "name"])
