"""SQLAlchemy engine + session factory.

The engine is created on first use and can be discarded with ``reset_engine``
so a later call rereads ``get_settings()``. Importing this module must not
bind to whatever ``DB_NAME`` happened to be current at import time (issue #27).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# Session-scoped integration tests bind here. The Alembic round-trip walk
# uses a different database name (see conftest.MIGRATION_DB_NAME) so it
# cannot drop this one mid-suite.
TEST_DATABASE_NAME = "portfonia_test_roundtrip"

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the cached engine, creating it from current settings if needed."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            echo=False,
        )
        _session_factory = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine so the next ``get_engine()`` rereads settings."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def SessionLocal() -> Session:
    """Open a session against the current engine.

    Under pytest, refuse anything other than ``TEST_DATABASE_NAME`` so a
    forgotten mock cannot write the developer's ``portfonia_dev`` database.
    """
    settings = get_settings()
    if os.environ.get("PYTEST_CURRENT_TEST") and settings.DB_NAME != TEST_DATABASE_NAME:
        raise RuntimeError(
            f"SessionLocal refused to bind to database {settings.DB_NAME!r} "
            "under pytest; use the db_session fixture or mock SessionLocal"
        )
    get_engine()
    assert _session_factory is not None
    return _session_factory()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a DB session and ensures it closes."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
