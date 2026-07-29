from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def createEngine(
    databaseUrl: str,
    *,
    echo: bool = False,
    migrate: bool = True,
) -> Engine:
    engine = create_engine(databaseUrl, echo=echo, pool_pre_ping=True)
    if migrate:
        from .migrations import upgradeDatabase

        try:
            upgradeDatabase(engine)
        except Exception:
            engine.dispose()
            raise
    return engine


def createSessionFactory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def sessionScope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
