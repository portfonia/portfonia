"""Shared pytest fixtures.

`alembic_cfg`: provisions a throwaway Postgres database against the running
portfonia-postgres container, wires Alembic to it via DB_NAME override, and
drops the database on teardown. Each test that consumes this fixture runs
against a pristine schema.

`db_session` / `app_client`: build on top of `alembic_cfg` to provide a live
SQLAlchemy session and a FastAPI TestClient wired to the test database.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import get_settings
from app.core.deps import get_current_user_id
from app.main import app

TEST_DB_NAME = "portfonia_test_roundtrip"
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Modules that import these by name (`from x import y`), each needing its own patch.
_EXTERNAL_NOTIFY_MODULES = (
    "app.services.report_generator",
    "app.tasks.report_tasks",
    "app.tasks.capture_tasks",
    "app.tasks.backup_tasks",
)


@pytest.fixture(autouse=True)
def _no_external_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test hit the real Resend/GitHub APIs.

    2026-06-19: `send_ops_alert` was unmocked, and `test_report_generator.py`
    uses a fixed historical `_TODAY = date(2026, 6, 4)` that always trips the
    FX-staleness check against the real current date. Three same-day pytest
    runs sent 42 real "FX rates stale" emails to the admin inbox. Individual
    tests may still re-patch these within a `with` block to assert call args —
    that only shadows this default for the duration of the `with` block.
    """
    for module in _EXTERNAL_NOTIFY_MODULES:
        monkeypatch.setattr(f"{module}.send_ops_alert", MagicMock(), raising=False)
        monkeypatch.setattr(f"{module}.create_bug_report", MagicMock(), raising=False)
    monkeypatch.setattr(
        "app.services.report_generator.send_report_email", MagicMock(return_value=True)
    )


def _admin_engine() -> Engine:
    s = get_settings()
    url = (
        f"postgresql+psycopg://{s.DB_USER}:{s.DB_PASSWORD.get_secret_value()}"
        f"@{s.DB_HOST}:{s.DB_PORT}/postgres"
    )
    return create_engine(url, isolation_level="AUTOCOMMIT")


def _drop_test_db(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :db AND pid <> pg_backend_pid()"
            ),
            {"db": TEST_DB_NAME},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch) -> Generator[Config, None, None]:
    admin = _admin_engine()
    _drop_test_db(admin)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))

    monkeypatch.setenv("DB_NAME", TEST_DB_NAME)
    get_settings.cache_clear()

    yield Config("alembic.ini")

    get_settings.cache_clear()
    _drop_test_db(admin)
    admin.dispose()


@pytest.fixture
def db_session(
    alembic_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> Generator[Session, None, None]:
    """Migrate the test DB to head and yield a live session.

    Isolation comes from `alembic_cfg` dropping the throwaway database after each
    test, not from a per-test rollback — committed writes are discarded with the
    DB on teardown.
    """
    command.upgrade(alembic_cfg, "head")
    s = get_settings()
    engine = create_engine(
        f"postgresql+psycopg://{s.DB_USER}:{s.DB_PASSWORD.get_secret_value()}"
        f"@{s.DB_HOST}:{s.DB_PORT}/{TEST_DB_NAME}",
        poolclass=NullPool,
    )
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def app_client(db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient with DB and user-id dependencies overridden for the test DB."""
    from app.core.database import get_session

    def _override_session() -> Generator[Session, None, None]:
        yield db_session

    def _override_user_id() -> uuid.UUID:
        return TEST_USER_ID

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user_id] = _override_user_id
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
