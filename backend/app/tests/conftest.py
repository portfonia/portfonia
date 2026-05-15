"""Shared pytest fixtures.

`alembic_cfg`: provisions a throwaway Postgres database against the running
portfonia-postgres container, wires Alembic to it via DB_NAME override, and
drops the database on teardown. Each test that consumes this fixture runs
against a pristine schema.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import get_settings

TEST_DB_NAME = "portfonia_test_roundtrip"


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
