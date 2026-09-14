"""Independent SQLAlchemy engine and session factory.

Importing this module must not bind Portfonia's Base or SessionLocal.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from vigil_app.core.config import get_settings

TEST_DATABASE_NAME = f"vigil_test_roundtrip_{os.getpid()}"

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
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
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def SessionLocal() -> Session:
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
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
