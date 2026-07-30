from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import Mock, patch

import pytest

from syn.migrations import (
    BASE_REVISION,
    EXPECTED_DOMAINS,
    EXPECTED_TABLES,
    PartialSchemaError,
    _hasLegacySchema,
    _hasVersionTable,
    _validateLegacySchema,
    alembicConfig,
    currentRevision,
    upgradeDatabase,
)


def inspector(*, tables: set[str], hasVersion: bool = False, hasRegistry: bool = False):
    value = Mock()
    value.get_table_names.return_value = list(tables)
    value.has_table.side_effect = lambda name: {
        "alembicVersion": hasVersion,
        "objectRegistry": hasRegistry,
    }.get(name, False)
    return value


def test_alembic_config_points_to_packaged_environment() -> None:
    config = alembicConfig()
    location = config.get_main_option("script_location")
    assert location.endswith("syn\\alembic") or location.endswith("syn/alembic")


def test_schema_presence_helpers_use_expected_table_names() -> None:
    connection = Mock()
    inspected = Mock()
    inspected.has_table.side_effect = lambda name: name == "alembicVersion"
    with patch("syn.migrations.inspect", return_value=inspected):
        assert _hasVersionTable(connection) is True
        assert _hasLegacySchema(connection) is False
    assert inspected.has_table.call_args_list[0].args == ("alembicVersion",)
    assert inspected.has_table.call_args_list[1].args == ("objectRegistry",)


def test_complete_legacy_schema_validation_passes() -> None:
    connection = Mock()
    connection.scalars.return_value = list(EXPECTED_DOMAINS)
    with patch("syn.migrations.inspect", return_value=inspector(tables=set(EXPECTED_TABLES))):
        _validateLegacySchema(connection)


def test_partial_legacy_schema_reports_missing_tables_and_domains() -> None:
    connection = Mock()
    connection.scalars.return_value = ["slug"]
    with (
        patch("syn.migrations.inspect", return_value=inspector(tables={"objectRegistry"})),
        pytest.raises(PartialSchemaError) as caught,
    ):
        _validateLegacySchema(connection)
    message = str(caught.value)
    assert "missing tables" in message
    assert "missing domains" in message


def test_upgrade_empty_database_runs_head_without_stamp() -> None:
    connection = Mock()
    engine = Mock()
    engine.begin.return_value = nullcontext(connection)
    with (
        patch("syn.migrations._hasLegacySchema", return_value=False),
        patch("syn.migrations._hasVersionTable", return_value=False),
        patch("syn.migrations.command.stamp") as stamp,
        patch("syn.migrations.command.upgrade") as upgrade,
    ):
        upgradeDatabase(engine)
    stamp.assert_not_called()
    upgrade.assert_called_once()
    assert upgrade.call_args.args[1] == "head"


def test_upgrade_complete_unversioned_schema_stamps_then_upgrades() -> None:
    connection = Mock()
    engine = Mock()
    engine.begin.return_value = nullcontext(connection)
    with (
        patch("syn.migrations._hasLegacySchema", return_value=True),
        patch("syn.migrations._hasVersionTable", return_value=False),
        patch("syn.migrations._validateLegacySchema") as validate,
        patch("syn.migrations.command.stamp") as stamp,
        patch("syn.migrations.command.upgrade") as upgrade,
    ):
        upgradeDatabase(engine)
    validate.assert_called_once_with(connection)
    assert stamp.call_args.args[1] == BASE_REVISION
    assert upgrade.call_args.args[1] == "head"


def test_upgrade_versioned_schema_does_not_stamp() -> None:
    connection = Mock()
    engine = Mock()
    engine.begin.return_value = nullcontext(connection)
    with (
        patch("syn.migrations._hasLegacySchema", return_value=True),
        patch("syn.migrations._hasVersionTable", return_value=True),
        patch("syn.migrations.command.stamp") as stamp,
        patch("syn.migrations.command.upgrade") as upgrade,
    ):
        upgradeDatabase(engine, "20260729_0001")
    stamp.assert_not_called()
    assert upgrade.call_args.args[1] == "20260729_0001"


def test_current_revision_is_none_without_version_table() -> None:
    connection = Mock()
    engine = Mock()
    engine.connect.return_value = nullcontext(connection)
    with patch("syn.migrations._hasVersionTable", return_value=False):
        assert currentRevision(engine) is None


def test_current_revision_reads_migration_context() -> None:
    connection = Mock()
    engine = Mock()
    engine.connect.return_value = nullcontext(connection)
    context = Mock()
    context.get_current_revision.return_value = BASE_REVISION
    with (
        patch("syn.migrations._hasVersionTable", return_value=True),
        patch("syn.migrations.MigrationContext.configure", return_value=context) as configure,
    ):
        assert currentRevision(engine) == BASE_REVISION
    context.get_current_revision.assert_called_once_with()
    configure.assert_called_once_with(
        connection,
        opts={"version_table": "alembicVersion"},
    )
