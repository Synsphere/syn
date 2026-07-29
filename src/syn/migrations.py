from __future__ import annotations

from importlib.resources import as_file, files

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, inspect, text


BASE_REVISION = "20260729_0001"
EXPECTED_TABLES = frozenset(
    {
        "objectRegistry",
        "types",
        "things",
        "events",
        "embers",
        "cogs",
        "enigmas",
        "pondles",
        "snugs",
        "oms",
        "objectTypes",
        "eventTargets",
        "objectRelationships",
        "objectChronicle",
    }
)
EXPECTED_DOMAINS = frozenset(
    {
        "slug",
        "objectRef",
        "typeRef",
        "thingRef",
        "eventRef",
        "emberRef",
        "cogRef",
        "enigmaRef",
        "pondleRef",
        "snugRef",
        "omRef",
        "observationRef",
        "balanceRef",
    }
)


class PartialSchemaError(RuntimeError):
    pass


def alembicConfig() -> Config:
    migrations = files("syn").joinpath("alembic")
    with as_file(migrations) as migrationsPath:
        config = Config()
        config.set_main_option("script_location", str(migrationsPath))
        return config


def _hasVersionTable(connection: Connection) -> bool:
    return inspect(connection).has_table("alembicVersion")


def _hasLegacySchema(connection: Connection) -> bool:
    return inspect(connection).has_table("objectRegistry")


def _validateLegacySchema(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    missingTables = EXPECTED_TABLES - tables
    domains = set(
        connection.scalars(
            text(
                "SELECT t.typname "
                "FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typtype = 'd' AND n.nspname = current_schema()"
            )
        )
    )
    missingDomains = EXPECTED_DOMAINS - domains
    if missingTables or missingDomains:
        raise PartialSchemaError(
            "Refusing to stamp a partial unversioned Syn schema; "
            f"missing tables={sorted(missingTables)}, "
            f"missing domains={sorted(missingDomains)}"
        )


def upgradeDatabase(engine: Engine, revision: str = "head") -> None:
    config = alembicConfig()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if _hasLegacySchema(connection) and not _hasVersionTable(connection):
            _validateLegacySchema(connection)
            command.stamp(config, BASE_REVISION)
        command.upgrade(config, revision)


def currentRevision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        if not _hasVersionTable(connection):
            return None
        return MigrationContext.configure(connection).get_current_revision()
