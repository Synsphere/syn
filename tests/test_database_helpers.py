from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from syn.database import createEngine, createSessionFactory, sessionScope


def test_create_engine_uses_health_checked_pool_and_echo() -> None:
    sentinel = object()
    with patch("syn.database.create_engine", return_value=sentinel) as factory:
        result = createEngine("postgresql+psycopg://localhost/syn", echo=True, migrate=False)
    assert result is sentinel
    factory.assert_called_once_with(
        "postgresql+psycopg://localhost/syn", echo=True, pool_pre_ping=True
    )


def test_create_engine_automatically_runs_migrations_by_default() -> None:
    engine = Mock()
    with (
        patch("syn.database.create_engine", return_value=engine),
        patch("syn.migrations.upgradeDatabase") as upgrade,
    ):
        assert createEngine("postgresql+psycopg://localhost/syn") is engine
    upgrade.assert_called_once_with(engine)


def test_create_engine_disposes_engine_when_migration_fails() -> None:
    engine = Mock()
    with (
        patch("syn.database.create_engine", return_value=engine),
        patch("syn.migrations.upgradeDatabase", side_effect=RuntimeError("migration failed")),
        pytest.raises(RuntimeError, match="migration failed"),
    ):
        createEngine("postgresql+psycopg://localhost/syn")
    engine.dispose.assert_called_once_with()


def test_create_session_factory_binds_engine_and_disables_expiry() -> None:
    engine = Mock()
    with patch("syn.database.sessionmaker") as maker:
        createSessionFactory(engine)
    maker.assert_called_once_with(bind=engine, expire_on_commit=False)


def test_session_scope_commits_and_closes_on_success() -> None:
    session = Mock()
    factory = Mock(return_value=session)
    with sessionScope(factory) as yielded:
        assert yielded is session
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()
    session.close.assert_called_once_with()


def test_session_scope_rolls_back_closes_and_reraises_on_error() -> None:
    session = Mock()
    factory = Mock(return_value=session)
    with pytest.raises(RuntimeError, match="failure"):
        with sessionScope(factory):
            raise RuntimeError("failure")
    session.commit.assert_not_called()
    session.rollback.assert_called_once_with()
    session.close.assert_called_once_with()


def test_session_scope_closes_if_commit_itself_fails() -> None:
    session = Mock()
    session.commit.side_effect = RuntimeError("commit failed")
    factory = Mock(return_value=session)
    with pytest.raises(RuntimeError, match="commit failed"):
        with sessionScope(factory):
            pass
    session.rollback.assert_called_once_with()
    session.close.assert_called_once_with()
