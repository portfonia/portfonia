"""Shared pytest fixtures.

`session_test_db`: provisions one throwaway Postgres database for the whole
pytest session, migrates it to head once, and drops it at teardown (issue #26).

`db_session` / `app_client`: open a connection-level transaction + SAVEPOINT
on that database. ``commit()`` in a test only releases the SAVEPOINT; the
outer transaction rolls back when the test ends, so committed writes do not
leak. ``SessionLocal()`` is rebound to the same connection so task code that
opens its own session still participates (issue #27).

`alembic_cfg`: a *separate* throwaway database for the Alembic
upgrade/downgrade walk. It must not share a name with ``session_test_db``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from alembic import command
from app.core.config import get_settings
from app.core.database import TEST_DATABASE_NAME, get_engine, reset_engine
from app.core.deps import Principal, current_principal
from app.main import app
from app.models.holding import Holding
from app.models.report import Report
from app.models.user import User
from app.services.window_data import BOOTSTRAP_WATERMARK

TEST_DB_NAME = TEST_DATABASE_NAME
# Same PID-suffixing rationale as TEST_DATABASE_NAME (issue #152) — this is a
# separate database from TEST_DB_NAME (see module docstring), so it needs its
# own unique suffix, not a reuse of TEST_DATABASE_NAME's.
MIGRATION_DB_NAME = f"portfonia_test_alembic_{os.getpid()}"
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Ring 1 stage-A shared UAT fixture (design doc §7.1, Hermes/Portfonia/Docs/
# Ring 1-A design.md, issue #128) — reused across the A1/A2/A3/A4 checkpoints
# so each doesn't build its own ad hoc multi-user holdings set.
U1_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
U2_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
U3_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a3")

# Modules that import these by name (`from x import y`), each needing its own patch.
_EXTERNAL_NOTIFY_MODULES = (
    "app.services.report_generator",
    "app.services.ticker_intel",
    "app.tasks.report_tasks",
    "app.tasks.capture_tasks",
    "app.tasks.backup_tasks",
    "app.tasks.cache_tasks",
    "app.tasks.admin_tasks",
)


@pytest.fixture(autouse=True)
def _rate_limit_memory(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Every test gets a fresh in-memory counter. A live Redis would leak
    buckets across tests and 503 if the daemon is down (issue #190).

    Also stub `send_admin_alert_task.delay` so a 429 cannot enqueue real
    Celery work against the local broker. Tests that assert call counts
    re-patch `.delay` and shadow this mock.
    """
    from app.core.rate_limit import InMemoryBackend, set_backend

    set_backend(InMemoryBackend())
    monkeypatch.setattr("app.core.rate_limit.send_admin_alert_task.delay", MagicMock())
    yield
    set_backend(None)


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
        os.environ.pop("DB_NAME", None)
    else:
        os.environ["DB_NAME"] = previous
    get_settings.cache_clear()
    reset_engine()


@pytest.fixture(scope="session")
def session_test_db() -> Generator[None, None, None]:
    """Create and migrate the integration test database once per pytest session."""
    admin = _admin_engine()
    _create_test_db(admin, TEST_DB_NAME)

    previous = os.environ.get("DB_NAME")
    os.environ["DB_NAME"] = TEST_DB_NAME
    get_settings.cache_clear()
    reset_engine()
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield
    finally:
        _restore_db_name(previous)
        _drop_test_db(admin, TEST_DB_NAME)
        admin.dispose()


@pytest.fixture
def alembic_cfg() -> Generator[Config, None, None]:
    """Empty throwaway DB for the Alembic revision walk — not the session DB."""
    admin = _admin_engine()
    _create_test_db(admin, MIGRATION_DB_NAME)

    previous = os.environ.get("DB_NAME")
    os.environ["DB_NAME"] = MIGRATION_DB_NAME
    get_settings.cache_clear()
    reset_engine()
    try:
        yield Config("alembic.ini")
    finally:
        _restore_db_name(previous)
        _drop_test_db(admin, MIGRATION_DB_NAME)
        admin.dispose()


@pytest.fixture
def db_session(
    session_test_db: None, monkeypatch: pytest.MonkeyPatch
) -> Generator[Session, None, None]:
    """Yield a live session whose commits roll back with the test.

    Isolation comes from an outer transaction + SAVEPOINT, not from dropping
    the database. ``SessionLocal()`` is pointed at the same connection so a
    forgotten task-level session cannot commit past the rollback.
    """
    connection = get_engine().connect()
    outer = connection.begin()

    def _session_local() -> Session:
        return Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
            autoflush=False,
            autocommit=False,
        )

    monkeypatch.setattr("app.core.database.SessionLocal", _session_local)

    session = _session_local()
    try:
        yield session
    finally:
        session.close()
        if outer.is_active:
            outer.rollback()
        connection.close()


@pytest.fixture
def three_user_holdings(db_session: Session) -> dict[str, uuid.UUID]:
    """U1/U2/U3 holdings (design doc §7.1) shared across Ring 1 stage-A
    checkpoints. Ticker/asset_class choices, verbatim from the design doc:

    U1: NVDA, AAPL, QQQM, 110011 (offshore fund), cash — baseline user,
        covers the auto/manual and fund paths.
    U2: NVDA, AAPL, 0700.HK, USD cash — overlaps U1 on 2 tickers (shared L0
        compute); AAPL is classified EQUITY_US_TECH here vs U1's STOCK, so
        the SAME identifier's SAME global move can clear one user's
        threshold and not the other's (design doc §3.3).
    U3: 513650.SS, 019547, SGOL — zero overlap with U1/U2, to prove
        isolation doesn't degenerate into "nobody's report has anything".

    QQQM/110011/0700.HK/513650.SS/019547 intentionally get no price_snapshot
    rows in this fixture alone — tests that need them anomalous add their
    own snapshots; tests that don't just exercise the "holding present, no
    usable baseline" path for free.
    """

    def _h(**kwargs: object) -> Holding:
        defaults: dict[str, object] = {"pricing_mode": "auto", "currency": "USD"}
        return Holding(**{**defaults, **kwargs})

    def _u(user_id: uuid.UUID, email: str) -> User:
        return User(
            id=user_id,
            auth_provider="supabase",
            auth_subject=f"sub-{user_id}",
            email=email,
            status="active",
            locale="zh",
            base_currency="USD",
            report_cadence="mwf",
        )

    db_session.add_all(
        [
            _u(U1_USER_ID, "u1@example.com"),
            _u(U2_USER_ID, "u2@example.com"),
            _u(U3_USER_ID, "u3@example.com"),
        ]
    )
    # These three are stand-ins for existing books, not brand-new signups.
    # Seed a DONE report at the historical baseline so A1-A4 keep a stable
    # window; new-user cold start is covered by test_window_data.py.
    for uid in (U1_USER_ID, U2_USER_ID, U3_USER_ID):
        db_session.add(
            Report(
                user_id=uid,
                report_date=date(2026, 6, 1),
                report_type="incremental",
                session_node="fixture_seed",
                status="success",
                report_md="fixture watermark seed",
                period_start=BOOTSTRAP_WATERMARK,
                period_end=BOOTSTRAP_WATERMARK,
            )
        )
    db_session.add_all(
        [
            _h(user_id=U1_USER_ID, name="NVIDIA", ticker="NVDA", asset_class="EQUITY_US_TECH"),
            _h(user_id=U1_USER_ID, name="Apple", ticker="AAPL", asset_class="STOCK"),
            _h(
                user_id=U1_USER_ID,
                name="Invesco QQQM",
                ticker="QQQM",
                asset_class="EQUITY_US_BROAD",
            ),
            _h(
                user_id=U1_USER_ID,
                name="Offshore Fund",
                fund_code="110011",
                currency="CNY",
                asset_class="EQUITY_CN",
            ),
            _h(
                user_id=U1_USER_ID,
                name="Cash",
                pricing_mode="manual",
                asset_class="CASH_EQUIV",
                current_value=Decimal("1000"),
            ),
            _h(user_id=U2_USER_ID, name="NVIDIA", ticker="NVDA", asset_class="EQUITY_US_TECH"),
            _h(user_id=U2_USER_ID, name="Apple", ticker="AAPL", asset_class="EQUITY_US_TECH"),
            _h(
                user_id=U2_USER_ID,
                name="Tencent",
                ticker="0700.HK",
                currency="HKD",
                asset_class="EQUITY_CN",
            ),
            _h(
                user_id=U2_USER_ID,
                name="Cash",
                pricing_mode="manual",
                asset_class="CASH_EQUIV",
                current_value=Decimal("500"),
            ),
            _h(
                user_id=U3_USER_ID,
                name="CSI 500 ETF",
                ticker="513650.SS",
                currency="CNY",
                asset_class="EQUITY_BROAD",
            ),
            _h(
                user_id=U3_USER_ID,
                name="Gold Fund",
                fund_code="019547",
                currency="CNY",
                asset_class="PRECIOUS_METALS",
            ),
            _h(user_id=U3_USER_ID, name="Gold ETF", ticker="SGOL", asset_class="PRECIOUS_METALS"),
        ]
    )
    db_session.flush()
    return {"U1": U1_USER_ID, "U2": U2_USER_ID, "U3": U3_USER_ID}


@pytest.fixture
def app_client(db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient with DB and identity dependencies overridden for the test DB.

    Only `current_principal` needs overriding (PR #181 review): every
    identity-bearing route depends on it now, not on `get_current_user_id`
    directly — `current_principal` calls that as a plain function, not a
    FastAPI sub-dependency, so overriding it separately would do nothing.
    """
    from app.core.database import get_session

    def _override_session() -> Generator[Session, None, None]:
        yield db_session

    def _override_principal() -> Principal:
        return Principal(user_id=TEST_USER_ID)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_principal] = _override_principal
    try:
        # Match production uvicorn --proxy-headers so XFF rewrites
        # request.client without an app-level parser (issue #190).
        yield TestClient(ProxyHeadersMiddleware(app, trusted_hosts="*"))
    finally:
        app.dependency_overrides.clear()
