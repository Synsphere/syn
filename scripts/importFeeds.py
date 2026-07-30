from __future__ import annotations

import argparse
from pathlib import Path

from syn import createEngine, importFeedBundle, loadConfig


def parseArguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import the deduplicated global feed research bundle into Syn."
    )
    parser.add_argument("bundle", type=Path, help="Path to global_feed_research_csv_bundle.zip")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Syn configuration file (default: config.toml)",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parseArguments()
    config = loadConfig(arguments.config)
    engine = createEngine(
        config.database.url,
        echo=config.database.echo,
        migrate=config.database.autoMigrate,
    )
    try:
        with engine.begin() as connection:
            result = importFeedBundle(connection, arguments.bundle)
    finally:
        engine.dispose()

    print(f"Source rows: {result.sourceCount}")
    print(f"Unique feeds: {result.uniqueCount}")
    print(f"Duplicate rows merged: {result.duplicateCount}")
    print(f"Things inserted: {result.insertedCount}")
    print(f"Things updated: {result.updatedCount}")
    print(f"Direct type classifications: {result.classificationCount}")


if __name__ == "__main__":
    main()
