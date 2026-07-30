from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from syn import MASTER_DATASET, createEngine, currentRevision, loadConfig
from syn.feeds import KNOWLEDGE_FIELDS


def parseArguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the installed Syn feed dataset.")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    return parser.parse_args()


def main() -> None:
    arguments = parseArguments()
    config = loadConfig(arguments.config)
    engine = createEngine(
        config.database.url,
        echo=config.database.echo,
        migrate=config.database.autoMigrate,
    )
    requiredKeys = [*KNOWLEDGE_FIELDS.values(), "description"]
    try:
        with engine.connect() as connection:
            summary = dict(
                connection.execute(
                    text(
                        "SELECT count(*) AS \"feedCount\", "
                        "count(DISTINCT knowledge ->> 'canonicalUrl') AS \"canonicalCount\", "
                        "count(*) FILTER (WHERE coalesce(knowledge ->> 'description', '') = '') "
                        "AS \"missingDescriptions\", "
                        "count(*) FILTER (WHERE NOT knowledge ?& CAST(:keys AS text[])) "
                        "AS \"missingFields\", "
                        "sum(jsonb_array_length(knowledge -> 'sourceRowNumbers')) AS \"sourceRows\", "
                        "count(*) FILTER (WHERE jsonb_array_length(knowledge -> 'duplicateUrls') > 1) "
                        "AS \"duplicateGroups\" "
                        "FROM things WHERE knowledge ->> 'sourceDataset' = :dataset"
                    ),
                    {"dataset": MASTER_DATASET, "keys": requiredKeys},
                ).mappings().one()
            )
            summary["feedTypeCount"] = connection.scalar(
                text("SELECT count(*) FROM types WHERE knowledge ->> 'ontology' = 'syn-feeds'")
            )
            summary["directClassifications"] = connection.scalar(
                text(
                    'SELECT count(*) FROM "objectTypes" classification '
                    'JOIN things thing ON thing.id = classification."objectRef" '
                    "JOIN types type ON type.id = classification.\"typeRef\" "
                    "WHERE thing.knowledge ->> 'sourceDataset' = :dataset "
                    "AND type.knowledge ->> 'ontology' = 'syn-feeds'"
                ),
                {"dataset": MASTER_DATASET},
            )
            summary["withoutInheritedFeedType"] = connection.scalar(
                text(
                    "SELECT count(*) FROM things "
                    "WHERE knowledge ->> 'sourceDataset' = :dataset "
                    'AND NOT "objectHasType"('
                    'id::"objectRef", \'syn://type/feed\'::"typeRef")'
                ),
                {"dataset": MASTER_DATASET},
            )
            summary["questionDefinition"] = connection.scalar(
                text(
                    "SELECT knowledge ->> 'definition' FROM types "
                    "WHERE id = 'syn://type/question'"
                )
            )
            summary["revision"] = currentRevision(engine)
    finally:
        engine.dispose()

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
