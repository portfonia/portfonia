"""Vigil test fixtures.

Session-scoped isolated PostgreSQL (PID-suffixed names, issue #152 pattern)
plus a separate Alembic walk database. External mail/dispatch stays unimported.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alembic import command

_REPO_ROOT = Path(__file__).resolve().parents[4]
_VIGIL_BACKEND = Path(__file__).resolve().parents[2]


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _local_postgres_role() -> dict[str, str]:
    parsed = _parse_env_file(_REPO_ROOT / ".env.local")
    return {
        "host": os.environ.get("VIGIL_DB_HOST") or parsed.get("DB_HOST", "localhost"),
        "port": os.environ.get("VIGIL_DB_PORT") or parsed.get("DB_PORT", "5432"),
        "user": os.environ.get("VIGIL_DB_USER") or parsed.get("DB_USER", "portfonia"),
        "password": os.environ.get("VIGIL_DB_PASSWORD") or parsed.get("DB_PASSWORD", ""),
    }


def required_vigil_env() -> dict[str, str]:
    role = _local_postgres_role()
    return {
        "VIGIL_APP_ENV": "test",
        "VIGIL_DB_HOST": role["host"],
        "VIGIL_DB_PORT": role["port"],
        "VIGIL_DB_NAME": f"vigil_test_roundtrip_{os.getpid()}",
        "VIGIL_DB_USER": role["user"],
        "VIGIL_DB_PASSWORD": role["password"],
        "VIGIL_REDIS_HOST": os.environ.get("VIGIL_REDIS_HOST", "localhost"),
        "VIGIL_REDIS_PORT": os.environ.get("VIGIL_REDIS_PORT", "6379"),
        "VIGIL_REDIS_DB": "15",
        "VIGIL_REDIS_KEY_PREFIX": "vigil-test",
        "VIGIL_CELERY_QUEUE_PREFIX": "vigil-test",
        "VIGIL_OBJECT_BUCKET": "vigil-test-objects",
        "VIGIL_OWNER_AUTH_SUBJECT": "owner-sub-test",
        "VIGIL_KEK": "k" * 32,
        "VIGIL_KEK_VERSION": "v1",
        "VIGIL_NOTIFICATION_KEY": "n" * 32,
        "VIGIL_NONCE_KEY": "c" * 32,
        "VIGIL_ALTCHA_HMAC_KEY": "a" * 32,
        "VIGIL_IDENTITY_SERVICE_TOKEN": "identity-token-test",
        "VIGIL_OPS_API_TOKEN": "ops-token-test",
        "VIGIL_RESEND_API_KEY": "re_test_not_real",
        "VIGIL_AUTH_ISSUER": "https://auth.test.invalid/auth/v1",
        "VIGIL_PORTFONIA_INTERNAL_BASE_URL": "http://backend:8000",
    }


def apply_required_env() -> None:
    for key, value in required_vigil_env().items():
        os.environ.setdefault(key, value)


apply_required_env()

from vigil_app.core.config import get_settings  # noqa: E402
from vigil_app.core.database import TEST_DATABASE_NAME, get_engine, reset_engine  # noqa: E402

TEST_DB_NAME = TEST_DATABASE_NAME
MIGRATION_DB_NAME = f"vigil_test_alembic_{os.getpid()}"
SENTINEL_DB_NAME = f"vigil_portfonia_sentinel_{os.getpid()}"


def _admin_engine() -> Engine:
    s = get_settings()
    url = (
        f"postgresql+psycopg://{s.DB_USER}:{s.DB_PASSWORD.get_secret_value()}"
        f"@{s.DB_HOST}:{s.DB_PORT}/postgres"
    )
    return create_engine(url, isolation_level="AUTOCOMMIT")


def _drop_test_db(engine: Engine, name: str) -> None:
    with engine.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :db AND pid <> pg_backend_pid()"
            ),
            {"db": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


def _create_test_db(admin: Engine, name: str) -> None:
    _drop_test_db(admin, name)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))


def _restore_db_name(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("VIGIL_DB_NAME", None)
    else:
        os.environ["VIGIL_DB_NAME"] = previous
    get_settings.cache_clear()
    reset_engine()


def _alembic_config() -> Config:
    cfg = Config(str(_VIGIL_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_VIGIL_BACKEND / "alembic"))
    return cfg


@pytest.fixture
def required_env() -> dict[str, str]:
    return required_vigil_env()


@pytest.fixture(scope="session")
def session_test_db() -> Generator[None, None, None]:
    admin = _admin_engine()
    _create_test_db(admin, TEST_DB_NAME)

    previous = os.environ.get("VIGIL_DB_NAME")
    os.environ["VIGIL_DB_NAME"] = TEST_DB_NAME
    get_settings.cache_clear()
    reset_engine()
    try:
        command.upgrade(_alembic_config(), "head")
        yield
    finally:
        _restore_db_name(previous)
        _drop_test_db(admin, TEST_DB_NAME)
        admin.dispose()


@pytest.fixture
def alembic_cfg() -> Generator[Config, None, None]:
    admin = _admin_engine()
    _create_test_db(admin, MIGRATION_DB_NAME)

    previous = os.environ.get("VIGIL_DB_NAME")
    os.environ["VIGIL_DB_NAME"] = MIGRATION_DB_NAME
    get_settings.cache_clear()
    reset_engine()
    try:
        yield _alembic_config()
    finally:
        _restore_db_name(previous)
        _drop_test_db(admin, MIGRATION_DB_NAME)
        admin.dispose()


@pytest.fixture
def sentinel_db() -> Generator[Engine, None, None]:
    """A fake Portfonia database that Vigil migrations must not touch."""
    admin = _admin_engine()
    _create_test_db(admin, SENTINEL_DB_NAME)
    s = get_settings()
    url = (
        f"postgresql+psycopg://{s.DB_USER}:{s.DB_PASSWORD.get_secret_value()}"
        f"@{s.DB_HOST}:{s.DB_PORT}/{SENTINEL_DB_NAME}"
    )
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE holdings (id integer PRIMARY KEY, note text NOT NULL)"))
        conn.execute(text("INSERT INTO holdings (id, note) VALUES (1, 'sentinel-row')"))
        conn.execute(text("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('13144ce0sent1')"))
    try:
        yield engine
    finally:
        engine.dispose()
        _drop_test_db(admin, SENTINEL_DB_NAME)
        admin.dispose()


@pytest.fixture
def db_session(
    session_test_db: None, monkeypatch: pytest.MonkeyPatch
) -> Generator[Session, None, None]:
    connection = get_engine().connect()
    outer = connection.begin()

    def _session_local() -> Session:
        return Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
            autoflush=False,
            autocommit=False,
        )

    monkeypatch.setattr("vigil_app.core.database.SessionLocal", _session_local)
    session = _session_local()
    try:
        yield session
    finally:
        session.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
