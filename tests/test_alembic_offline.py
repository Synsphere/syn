from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from alembic import command

from syn.migrations import alembicConfig


def test_offline_baseline_preserves_postgresql_percent_characters() -> None:
    config = alembicConfig()
    output = StringIO()
    config.output_buffer = output
    with patch("syn.config.loadConfig") as load:
        load.return_value.database.url = "postgresql+psycopg://localhost/syn"
        command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "'%[0-9A-Fa-f]{2}'" in sql
    assert "'%%[0-9A-Fa-f]{2}'" not in sql
    assert "cannot delete referenced local ObjectRef %" in sql
    assert "cannot delete referenced local ObjectRef %%" not in sql
