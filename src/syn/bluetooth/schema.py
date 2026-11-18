"""Bluetooth ontology over existing native objects; no new tables or migration."""

from syn.positions.schema import (
    metadata, primitive, typeParents, eventTargets, nodes, insert_ignore,
)
# The Cardano module already declares the common native Chronicle projection in
# position metadata. Reuse that Table object rather than declaring it twice.
from syn.cardano.schema import chronicle


BEACON_TYPE = "syn://type/bluetooth-beacon"
TRANSMISSION_TYPE = "syn://type/bluetooth-beacon-transmission-event"
RECEPTION_TYPE = "syn://type/bluetooth-reception-event"
LIFECYCLE_TYPE = "syn://type/bluetooth-lifecycle-event"
OBSERVATION_TYPE = "syn://type/bluetooth-advertising-observation-ember"

TYPE_DEFINITIONS = (
    ("bluetooth-beacon", "thing", "A Syn node's Bluetooth beacon service."),
    ("bluetooth-beacon-generation-event", "event", "A local Bluetooth beacon generation or advertising operation."),
    ("bluetooth-beacon-transmission-event", "bluetooth-beacon-generation-event", "A requested or backend-reported Bluetooth advertising transition; it does not prove reception over the air."),
    ("bluetooth-reception-event", "event", "A local adapter's observation of a Bluetooth advertisement."),
    ("bluetooth-lifecycle-event", "event", "A Bluetooth discovery, adapter lifecycle or observation-loss report."),
    ("bluetooth-advertising-observation-ember", "observation-result", "Source-qualified evidence of Bluetooth advertising, reception or loss; radio identity claims are unauthenticated."),
)


def seed_types(session):
    for slug, parent, definition in TYPE_DEFINITIONS:
        reference = "syn://type/" + slug
        insert_ignore(session, primitive["types"], {
            "id": reference, "name": slug,
            "knowledge": {"ontology": "syn-bluetooth", "ontologyVersion": 1,
                          "definition": definition},
        }, ["id"])
        insert_ignore(session, typeParents, {
            "childRef": reference, "parentRef": "syn://type/" + parent,
        }, ["childRef", "parentRef"])
