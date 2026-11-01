from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, create_engine

from syn.config import loadConfig
from syn.models import Base
from syn.communication import metadata as communicationMetadata
from syn.policy_adoption import metadata as policyMetadata
from syn.sqltypes import ObjectRefType, SlugType


config = context.config
target_metadata = [Base.metadata, communicationMetadata, policyMetadata]


def compareType(context_, inspectedColumn, metadataColumn, inspectedType, metadataType):
    if isinstance(metadataType, (ObjectRefType, SlugType)):
        return False
    return None


def configure(connection: Connection | None = None, *, url: str | None = None) -> None:
    options = dict(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        compare_type=compareType,
        compare_server_default=True,
        version_table="alembicVersion",
        include_schemas=False,
    )
    if connection is None:
        options["dialect_opts"] = {"paramstyle": "named"}
    context.configure(**options)


def runMigrationsOffline() -> None:
    configure(url=loadConfig().database.url)
    with context.begin_transaction():
        context.run_migrations()


def runMigrationsOnline() -> None:
    suppliedConnection = config.attributes.get("connection")
    if suppliedConnection is not None:
        configure(suppliedConnection)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(loadConfig().database.url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            configure(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    runMigrationsOffline()
else:
    runMigrationsOnline()
