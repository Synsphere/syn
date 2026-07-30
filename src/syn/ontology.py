from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable


ONTOLOGY_VERSION = 1
QUESTION_DEFINITION = (
    "A proposition which can be answered, either yes or no, or with any other "
    "kind of response."
)


@dataclass(frozen=True, slots=True)
class TypeDefinition:
    slug: str
    parents: tuple[str, ...]
    module: str
    definition: str | None = None

    @property
    def name(self) -> str:
        head, *tail = self.slug.split("-")
        return head + "".join(part.capitalize() for part in tail)

    @property
    def objectRef(self) -> str:
        return f"syn://type/{self.slug}"


def _type(
    slug: str,
    *parents: str,
    module: str,
    definition: str | None = None,
) -> TypeDefinition:
    return TypeDefinition(slug, tuple(parents), module, definition)


TYPE_DEFINITIONS: tuple[TypeDefinition, ...] = (
    # Upper ontology.
    _type("thing", module="upper", definition="The root type for referents classified by Syn."),
    _type("persistent-thing", "thing", module="upper"),
    _type("occurrent-thing", "thing", module="upper"),
    _type("dependent-thing", "thing", module="upper"),
    _type("abstract-thing", "thing", module="upper"),
    _type("collection", "thing", module="upper"),
    _type("aggregate", "collection", module="upper"),
    _type("system", "persistent-thing", module="upper"),
    _type("boundary", "dependent-thing", module="upper"),
    _type("relation", "abstract-thing", module="upper"),
    _type("agent", "persistent-thing", module="upper"),
    _type("spatial-thing", "persistent-thing", module="upper"),
    _type("temporal-thing", "thing", module="upper"),
    _type("information-object", "persistent-thing", module="upper"),
    _type("social-thing", "persistent-thing", module="upper"),
    _type("material-thing", "persistent-thing", module="upper"),
    _type("quality", "dependent-thing", module="upper"),
    _type("role", "dependent-thing", module="upper"),
    _type("disposition", "dependent-thing", module="upper"),
    _type("function", "disposition", module="upper"),

    # Material and natural things.
    _type("natural-object", "material-thing", module="material"),
    _type("artifact", "material-thing", module="material"),
    _type("infrastructure", "artifact", module="material"),
    _type("building", "artifact", "spatial-thing", module="material"),
    _type("facility", "artifact", "spatial-thing", module="material"),
    _type("device", "artifact", module="material"),
    _type("instrument", "device", module="material"),
    _type("tool", "artifact", module="material"),
    _type("machine", "device", module="material"),
    _type("vehicle", "artifact", module="material"),
    _type("vessel", "vehicle", module="material"),
    _type("container", "artifact", module="material"),
    _type("product-model", "abstract-thing", module="material"),
    _type("product-item", "artifact", module="material"),
    _type("material", "material-thing", module="material"),
    _type("substance", "material", module="material"),
    _type("mixture", "substance", module="material"),
    _type("chemical-entity", "material", module="material"),
    _type("food", "material", module="material"),
    _type("drug", "material", module="material"),
    _type("energy-carrier", "material", module="material"),
    _type("waste", "material", module="material"),
    _type("astronomical-object", "natural-object", module="natural"),
    _type("planetary-body", "astronomical-object", module="natural"),
    _type("star", "astronomical-object", module="natural"),
    _type("geological-object", "natural-object", module="natural"),
    _type("mineral", "geological-object", "substance", module="natural"),
    _type("rock", "geological-object", module="natural"),
    _type("landform", "natural-object", "spatial-thing", module="natural"),
    _type("body-of-water", "natural-object", "spatial-thing", module="natural"),
    _type("atmospheric-object", "natural-object", module="natural"),
    _type("weather-system", "atmospheric-object", "system", module="natural"),
    _type("environmental-feature", "natural-object", "spatial-thing", module="natural"),

    # Biology.
    _type("organism", "material-thing", module="biology"),
    _type("human-organism", "organism", module="biology"),
    _type("animal-organism", "organism", module="biology"),
    _type("plant-organism", "organism", module="biology"),
    _type("fungal-organism", "organism", module="biology"),
    _type("microorganism", "organism", module="biology"),
    _type("person", "human-organism", "agent", module="biology"),
    _type("animal-agent", "animal-organism", "agent", module="biology"),
    _type("biological-population", "collection", module="biology"),
    _type("biological-community", "collection", module="biology"),
    _type("taxon", "abstract-thing", module="biology", definition="A classifying group of organisms, not an individual organism."),
    _type("species-taxon", "taxon", module="biology"),
    _type("breed", "taxon", module="biology"),
    _type("cultivar", "taxon", module="biology"),
    _type("strain", "taxon", module="biology"),
    _type("biological-structure", "material-thing", module="biology"),
    _type("organ", "biological-structure", module="biology"),
    _type("tissue", "biological-structure", module="biology"),
    _type("cell", "biological-structure", module="biology"),
    _type("cellular-component", "biological-structure", module="biology"),
    _type("gene", "biological-structure", "information-object", module="biology"),
    _type("protein", "biological-structure", "chemical-entity", module="biology"),
    _type("biomolecule", "biological-structure", "chemical-entity", module="biology"),
    _type("ecosystem", "system", "spatial-thing", module="biology"),
    _type("habitat", "spatial-thing", module="biology"),
    _type("pathological-condition", "dependent-thing", module="biology"),
    _type("disease", "pathological-condition", module="biology"),

    # Agents and social structures.
    _type("collective-agent", "agent", "collection", module="social"),
    _type("software-agent", "agent", "software-system", module="social"),
    _type("organization", "collective-agent", "social-thing", "system", module="social"),
    _type("group", "collection", "social-thing", module="social"),
    _type("community", "group", module="social"),
    _type("family", "group", module="social"),
    _type("household", "group", module="social"),
    _type("legal-entity", "social-thing", module="social"),
    _type("economic-agent", "agent", module="social"),
    _type("company", "organization", "legal-entity", "economic-agent", module="social"),
    _type("government-organization", "organization", module="social"),
    _type("educational-institution", "organization", module="social"),
    _type("religious-organization", "organization", module="social"),
    _type("military-organization", "organization", module="social"),
    _type("political-party", "organization", module="social"),
    _type("sports-organization", "organization", module="social"),
    _type("nonprofit-organization", "organization", module="social"),
    _type("institution", "social-thing", "system", module="social"),
    _type("social-system", "social-thing", "system", module="social"),
    _type("culture", "social-system", module="social"),
    _type("civilization", "social-system", module="social"),

    # Space and time.
    _type("place", "spatial-thing", module="space-time"),
    _type("geographic-feature", "place", module="space-time"),
    _type("geographic-region", "place", module="space-time"),
    _type("administrative-area", "geographic-region", module="space-time"),
    _type("jurisdiction", "geographic-region", "social-thing", module="space-time"),
    _type("settlement", "place", "social-system", module="space-time"),
    _type("city", "settlement", module="space-time"),
    _type("town", "settlement", module="space-time"),
    _type("village", "settlement", module="space-time"),
    _type("neighborhood", "place", "social-thing", module="space-time"),
    _type("site", "place", module="space-time"),
    _type("address", "information-object", module="space-time"),
    _type("route", "spatial-thing", module="space-time"),
    _type("road", "infrastructure", "route", module="space-time"),
    _type("transport-network", "infrastructure", "network", module="space-time"),
    _type("protected-area", "geographic-region", module="space-time"),
    _type("virtual-place", "spatial-thing", "information-object", module="space-time"),
    _type("geometry", "mathematical-object", "information-object", module="space-time"),
    _type("coordinate-reference-system", "abstract-thing", "system", module="space-time"),
    _type("instant", "temporal-thing", module="space-time"),
    _type("interval", "temporal-thing", module="space-time"),
    _type("duration", "quantity-value", module="space-time"),
    _type("date-time-description", "information-object", module="space-time"),
    _type("calendar-period", "interval", module="space-time"),
    _type("era", "interval", module="space-time"),
    _type("time-zone", "abstract-thing", module="space-time"),

    # Occurrents and activities.
    _type("process", "occurrent-thing", module="occurrent"),
    _type("activity", "process", module="occurrent"),
    _type("event", "occurrent-thing", module="occurrent"),
    _type("event-series", "collection", module="occurrent"),
    _type("event-edition", "event", module="occurrent"),
    _type("season", "interval", module="occurrent"),
    _type("sports-season", "season", "event-series", module="occurrent"),
    _type("interaction", "activity", module="occurrent"),
    _type("state", "occurrent-thing", module="occurrent"),
    _type("situation", "occurrent-thing", module="occurrent"),
    _type("change", "process", module="occurrent"),
    _type("transition", "change", module="occurrent"),
    _type("lifecycle", "process", module="occurrent"),
    _type("history", "process", module="occurrent"),
    _type("natural-process", "process", module="occurrent"),
    _type("physical-process", "natural-process", module="occurrent"),
    _type("chemical-process", "natural-process", module="occurrent"),
    _type("biological-process", "natural-process", module="occurrent"),
    _type("geological-process", "natural-process", module="occurrent"),
    _type("meteorological-process", "natural-process", module="occurrent"),
    _type("social-process", "process", module="occurrent"),
    _type("epistemic-activity", "activity", module="occurrent"),
    _type("communication", "interaction", module="occurrent"),
    _type("creation", "activity", module="occurrent"),
    _type("publication", "creation", "event", module="occurrent"),
    _type("performance", "activity", "event", module="occurrent"),
    _type("movement", "process", module="occurrent"),
    _type("transportation", "movement", "activity", module="occurrent"),
    _type("observation", "epistemic-activity", "event", module="occurrent"),
    _type("measurement", "observation", module="occurrent"),
    _type("experiment", "epistemic-activity", "event", module="occurrent"),
    _type("computational-process", "process", module="occurrent"),
    _type("computation", "computational-process", "activity", module="occurrent"),
    _type("decision", "epistemic-activity", "event", module="occurrent"),
    _type("transaction", "interaction", "event", module="occurrent"),
    _type("governance", "social-process", "activity", module="occurrent"),
    _type("legal-proceeding", "social-process", "event", module="occurrent"),
    _type("education", "social-process", "activity", module="occurrent"),
    _type("employment", "social-process", module="occurrent"),
    _type("competition", "activity", "event", module="occurrent"),
    _type("game", "activity", "event", module="occurrent"),
    _type("conflict", "social-process", module="occurrent"),
    _type("battle", "conflict", "event", module="occurrent"),
    _type("war", "conflict", module="occurrent"),
    _type("ceremony", "activity", "event", module="occurrent"),
    _type("election", "activity", "event", module="occurrent"),
    _type("meeting", "interaction", "event", module="occurrent"),
    _type("birth", "biological-process", "event", module="occurrent"),
    _type("death", "biological-process", "event", module="occurrent"),
    _type("disaster", "event", module="occurrent"),

    # Information, propositions, and bibliographic identity.
    _type("proposition", "information-object", module="information"),
    _type("question", "proposition", module="information", definition=QUESTION_DEFINITION),
    _type("assertion", "proposition", module="information"),
    _type("claim", "assertion", module="information"),
    _type("hypothesis", "proposition", module="information"),
    _type("prediction", "proposition", module="information"),
    _type("explanation", "proposition", module="information"),
    _type("argument", "information-object", module="information"),
    _type("theory", "proposition", module="information"),
    _type("conceptual-model", "information-object", "abstract-thing", module="information"),
    _type("plan", "information-object", module="information"),
    _type("specification", "information-object", module="information"),
    _type("procedure", "plan", module="information"),
    _type("method", "procedure", module="information"),
    _type("algorithm", "method", module="information"),
    _type("rule", "proposition", module="information"),
    _type("policy", "rule", module="information"),
    _type("standard", "specification", "rule", module="information"),
    _type("legal-instrument", "information-object", module="information"),
    _type("law", "rule", "legal-instrument", module="information"),
    _type("contract", "legal-instrument", module="information"),
    _type("license", "legal-instrument", module="information"),
    _type("treaty", "legal-instrument", module="information"),
    _type("record", "information-object", module="information"),
    _type("document", "information-object", module="information"),
    _type("message", "information-object", module="information"),
    _type("dataset", "information-object", "collection", module="information"),
    _type("database", "dataset", "system", module="information"),
    _type("schema", "information-object", module="information"),
    _type("ontology", "schema", module="information"),
    _type("source-material", "information-object", module="information"),
    _type("citation", "information-object", module="information"),
    _type("evidence-record", "record", module="information"),
    _type("provenance-record", "record", module="information"),
    _type("representation", "information-object", module="information"),
    _type("symbol", "representation", module="information"),
    _type("name", "symbol", module="information"),
    _type("identifier", "symbol", module="information"),
    _type("language", "abstract-thing", "system", module="information"),
    _type("work", "information-object", module="bibliographic"),
    _type("expression", "information-object", module="bibliographic"),
    _type("manifestation", "information-object", "collection", module="bibliographic"),
    _type("manifestation-item", "material-thing", module="bibliographic"),

    # Creative and cultural works.
    _type("creative-work", "work", module="creative"),
    _type("textual-work", "creative-work", module="creative"),
    _type("book-work", "textual-work", module="creative"),
    _type("article", "textual-work", module="creative"),
    _type("poem", "textual-work", module="creative"),
    _type("play", "textual-work", module="creative"),
    _type("visual-work", "creative-work", module="creative"),
    _type("image", "visual-work", module="creative"),
    _type("painting", "visual-work", module="creative"),
    _type("map", "visual-work", "representation", module="creative"),
    _type("musical-work", "creative-work", module="creative"),
    _type("song", "musical-work", module="creative"),
    _type("audio-recording", "expression", module="creative"),
    _type("podcast", "audio-recording", module="creative"),
    _type("audiovisual-work", "creative-work", module="creative"),
    _type("film", "audiovisual-work", module="creative"),
    _type("video", "audiovisual-work", module="creative"),
    _type("television-series", "audiovisual-work", "collection", module="creative"),
    _type("episode", "audiovisual-work", module="creative"),
    _type("game-work", "creative-work", module="creative"),
    _type("software-work", "creative-work", module="creative"),
    _type("performance-work", "creative-work", module="creative"),
    _type("architectural-work", "creative-work", module="creative"),
    _type("design", "creative-work", module="creative"),
    _type("cultural-heritage-object", "artifact", module="creative"),
    _type("folklore", "information-object", "social-thing", module="creative"),
    _type("mythological-entity", "information-object", module="creative"),
    _type("fictional-character", "information-object", module="creative"),
    _type("comics-character", "fictional-character", module="creative"),
    _type("film-character", "fictional-character", module="creative"),
    _type("television-character", "fictional-character", module="creative"),
    _type("animated-character", "fictional-character", module="creative"),

    # Computation and technology.
    _type("hardware-system", "system", "artifact", module="computing"),
    _type("software-system", "system", "information-object", module="computing"),
    _type("software-application", "software-system", module="computing"),
    _type("software-library", "software-system", module="computing"),
    _type("software-service", "software-system", "service", module="computing"),
    _type("computational-method", "method", module="computing"),
    _type("software-function", "algorithm", module="computing"),
    _type("script", "software-work", module="computing"),
    _type("program", "software-work", module="computing"),
    _type("workflow", "procedure", module="computing"),
    _type("machine-learning-model", "conceptual-model", "software-system", module="computing"),
    _type("execution", "computation", "event", module="computing"),
    _type("job", "execution", module="computing"),
    _type("api", "specification", module="computing"),
    _type("protocol", "procedure", "specification", module="computing"),
    _type("digital-object", "information-object", module="computing"),
    _type("file", "digital-object", module="computing"),
    _type("software-package", "software-work", module="computing"),
    _type("software-version", "expression", module="computing"),
    _type("vulnerability", "disposition", module="computing"),
    _type("computational-procedure", "procedure", "computational-method", module="computing"),

    # Formal and quantitative objects.
    _type("concept", "abstract-thing", module="formal"),
    _type("type", "concept", module="formal"),
    _type("property", "relation", module="formal"),
    _type("set", "collection", "abstract-thing", module="formal"),
    _type("bag", "collection", "abstract-thing", module="formal"),
    _type("sequence", "collection", "abstract-thing", module="formal"),
    _type("abstract-structure", "abstract-thing", module="formal"),
    _type("graph", "abstract-structure", module="formal"),
    _type("tree", "graph", module="formal"),
    _type("network", "graph", module="formal"),
    _type("hierarchy", "graph", module="formal"),
    _type("pattern", "abstract-thing", module="formal"),
    _type("mathematical-object", "abstract-thing", module="formal"),
    _type("number", "mathematical-object", module="formal"),
    _type("boolean", "abstract-thing", module="formal"),
    _type("algebraic-structure", "mathematical-object", "abstract-structure", module="formal"),
    _type("logical-system", "abstract-structure", module="formal"),
    _type("quantity-kind", "abstract-thing", module="formal"),
    _type("dimension", "abstract-thing", module="formal"),
    _type("unit", "abstract-thing", module="formal"),
    _type("quantity-value", "abstract-thing", module="formal"),
    _type("scalar", "mathematical-object", module="formal"),
    _type("vector", "mathematical-object", module="formal"),
    _type("tensor", "mathematical-object", module="formal"),
    _type("variable", "mathematical-object", module="formal"),
    _type("parameter", "variable", module="formal"),
    _type("metric", "mathematical-object", module="formal"),
    _type("probability", "quantity-value", module="formal"),
    _type("probability-distribution", "mathematical-object", module="formal"),

    # Scientific and epistemic objects.
    _type("observable-property", "property", module="epistemic"),
    _type("feature-of-interest-role", "role", module="epistemic"),
    _type("sensor", "instrument", module="epistemic"),
    _type("sampler", "instrument", module="epistemic"),
    _type("sample", "material-thing", module="epistemic"),
    _type("observation-result", "information-object", module="epistemic"),
    _type("measurement-result", "observation-result", module="epistemic"),
    _type("data-point", "observation-result", module="epistemic"),
    _type("study", "epistemic-activity", module="epistemic"),
    _type("scientific-model", "conceptual-model", module="epistemic"),
    _type("quality-measure", "metric", module="epistemic"),
    _type("uncertainty", "quality", module="epistemic"),
    _type("confidence", "quality", module="epistemic"),
    _type("error", "quality", module="epistemic"),
    _type("bias", "quality", module="epistemic"),
    _type("belief", "dependent-thing", module="epistemic"),
    _type("knowledge-state", "dependent-thing", module="epistemic"),

    # Economic and legal objects.
    _type("service", "social-thing", module="economic"),
    _type("brand", "information-object", module="economic"),
    _type("market", "social-system", module="economic"),
    _type("industry", "social-system", module="economic"),
    _type("currency", "system", "unit", module="economic"),
    _type("monetary-amount", "quantity-value", module="economic"),
    _type("account", "information-object", module="economic"),
    _type("financial-instrument", "legal-instrument", module="economic"),
    _type("security", "financial-instrument", module="economic"),
    _type("loan", "financial-instrument", module="economic"),
    _type("fund", "financial-instrument", module="economic"),
    _type("insurance-product", "financial-instrument", module="economic"),
    _type("legal-case", "information-object", module="legal"),
    _type("judgment", "proposition", "legal-instrument", module="legal"),
    _type("right", "dependent-thing", module="legal"),
    _type("duty", "dependent-thing", module="legal"),
    _type("authority", "dependent-thing", module="legal"),
    _type("obligation", "dependent-thing", module="legal"),
    _type("legal-title", "legal-instrument", module="legal"),
    _type("legal-claim", "claim", module="legal"),
    _type("crime", "activity", module="legal"),
    _type("sanction", "event", module="legal"),

    # Psychological, evaluative, and Syn-specific concepts.
    _type("goal", "dependent-thing", module="evaluation"),
    _type("intention", "dependent-thing", module="evaluation"),
    _type("preference", "dependent-thing", module="evaluation"),
    _type("desire", "dependent-thing", module="evaluation"),
    _type("emotion", "dependent-thing", module="evaluation"),
    _type("attitude", "dependent-thing", module="evaluation"),
    _type("commitment", "dependent-thing", module="evaluation"),
    _type("attachment", "dependent-thing", module="evaluation"),
    _type("investment", "commitment", module="evaluation"),
    _type("trust", "attitude", module="evaluation"),
    _type("reputation", "social-thing", module="evaluation"),
    _type("risk", "dependent-thing", module="evaluation"),
    _type("benefit", "quality", module="evaluation"),
    _type("harm", "quality", module="evaluation"),
    _type("evaluation", "epistemic-activity", module="evaluation"),
    _type("assessment", "evaluation", module="evaluation"),
    _type("knowledge-record", "record", module="syn"),
    _type("evaluation-record", "record", module="syn"),
    _type("attachment-record", "record", module="syn"),
    _type("karma-unit", "unit", module="syn"),
)


TYPE_BY_SLUG = {definition.slug: definition for definition in TYPE_DEFINITIONS}


def validateOntology(definitions: Iterable[TypeDefinition] = TYPE_DEFINITIONS) -> None:
    values = tuple(definitions)
    bySlug = {definition.slug: definition for definition in values}
    if len(bySlug) != len(values):
        raise ValueError("Ontology type slugs must be unique")
    if "thing" not in bySlug or bySlug["thing"].parents:
        raise ValueError("The ontology must contain a parentless thing root")

    for definition in values:
        if definition.slug != "thing" and not definition.parents:
            raise ValueError(f"{definition.slug} is not connected to the thing root")
        for parent in definition.parents:
            if parent not in bySlug:
                raise ValueError(f"{definition.slug} has unknown parent {parent}")
            if parent == definition.slug:
                raise ValueError(f"{definition.slug} cannot be its own parent")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(slug: str) -> None:
        if slug in visiting:
            raise ValueError(f"Ontology contains a cycle through {slug}")
        if slug in visited:
            return
        visiting.add(slug)
        for parent in bySlug[slug].parents:
            visit(parent)
        visiting.remove(slug)
        visited.add(slug)

    for slug in bySlug:
        visit(slug)


def _sqlLiteral(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _chunks[T](values: tuple[T, ...], size: int) -> Iterable[tuple[T, ...]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def ontologySqlStatements() -> tuple[str, ...]:
    statements: list[str] = []
    for chunk in _chunks(TYPE_DEFINITIONS, 50):
        rows: list[str] = []
        for definition in chunk:
            knowledge = {
                "module": definition.module,
                "ontology": "syn-core",
                "ontologyVersion": ONTOLOGY_VERSION,
            }
            if definition.definition is not None:
                knowledge["definition"] = definition.definition
            rows.append(
                "(" + ", ".join(
                    (
                        f'{_sqlLiteral(definition.objectRef)}::"typeRef"',
                        _sqlLiteral(definition.name),
                        f"{_sqlLiteral(json.dumps(knowledge, ensure_ascii=False))}::jsonb",
                    )
                ) + ")"
            )
        statements.append(
            "INSERT INTO types (id, name, knowledge) VALUES\n    "
            + ",\n    ".join(rows)
            + "\nON CONFLICT (id) DO UPDATE SET "
            "name = EXCLUDED.name, "
            "knowledge = coalesce(types.knowledge, '{}'::jsonb) || EXCLUDED.knowledge"
        )

    edges = tuple(
        (definition.objectRef, f"syn://type/{parent}")
        for definition in TYPE_DEFINITIONS
        for parent in definition.parents
    )
    for chunk in _chunks(edges, 100):
        rows = [
            f'({_sqlLiteral(child)}::"typeRef", {_sqlLiteral(parent)}::"typeRef")'
            for child, parent in chunk
        ]
        statements.append(
            'INSERT INTO "typeParents" ("childRef", "parentRef") VALUES\n    '
            + ",\n    ".join(rows)
            + '\nON CONFLICT ("childRef", "parentRef") DO NOTHING'
        )
    return tuple(statements)


validateOntology()
