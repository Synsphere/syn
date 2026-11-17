"""PostgreSQL session ownership across DB commit and external custody updates."""
from contextlib import contextmanager
import threading

from sqlalchemy import text

_test_locks = {}
_test_guard = threading.Lock()


@contextmanager
def ownership(sessions, key):
    with sessions() as session:
        engine = session.get_bind()
    if engine.dialect.name != "postgresql":
        # Process-local SQLite isolation for tests; not a distributed lease.
        with _test_guard:
            lock = _test_locks.setdefault(key, threading.RLock())
        with lock:
            yield
        return
    with engine.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key,0))"), {"key": key})
        connection.commit()
        if not acquired:
            raise RuntimeError("Another checkpoint writer owns this scope")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"), {"key": key})
            connection.commit()
