"""Lazy engine binding and test-DB isolation (issues #26 / #27)."""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.holding import Holding
from app.tests.conftest import TEST_USER_ID

_ISOLATION_MARKER = "issue-26-isolation-marker"


def test_get_engine_is_lazy_and_reset_rebinds(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import database

    database.reset_engine()
    monkeypatch.setenv("DB_NAME", database.TEST_DATABASE_NAME)
    get_settings.cache_clear()

    first = database.get_engine()
    assert first.url.database == database.TEST_DATABASE_NAME

    monkeypatch.setenv("DB_NAME", "portfonia_other_test")
    get_settings.cache_clear()
    assert database.get_engine() is first

    database.reset_engine()
    second = database.get_engine()
    assert second is not first
    assert second.url.database == "portfonia_other_test"

    database.reset_engine()
    get_settings.cache_clear()


def test_sessionlocal_refuses_non_test_database_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import database

    database.reset_engine()
    monkeypatch.setenv("DB_NAME", "portfonia_dev")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="under pytest"):
        database.SessionLocal()
    database.reset_engine()
    get_settings.cache_clear()


def test_session_test_db_fixture_is_session_scoped() -> None:
    from app.tests.conftest import session_test_db

    assert session_test_db._fixture_function_marker.scope == "session"


def test_session_test_db_upgrades_inside_try() -> None:
    from app.tests import conftest

    source = inspect.getsource(conftest.session_test_db)
    assert source.index("try:") < source.index("command.upgrade")


def test_db_session_does_not_reupgrade_per_test() -> None:
    from app.tests import conftest

    assert "command.upgrade" not in inspect.getsource(conftest.db_session)


def test_alembic_cfg_uses_a_dedicated_database() -> None:
    """Migration walk must not drop the session-scoped integration DB."""
    from app.tests import conftest

    source = inspect.getsource(conftest.alembic_cfg)
    assert "MIGRATION_DB_NAME" in source
    assert conftest.MIGRATION_DB_NAME != conftest.TEST_DB_NAME


def test_db_session_disables_autoflush(db_session: Session) -> None:
    assert db_session.autoflush is False


def test_commit_is_not_visible_outside_the_savepoint(db_session: Session) -> None:
    """A commit in db_session must not land on a connection outside the outer txn.

    This does not depend on another test having run first.
    """
    from sqlalchemy import text

    from app.core.database import get_engine

    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name=_ISOLATION_MARKER,
            pricing_mode="auto",
            currency="USD",
            asset_class="STOCK",
            shares=Decimal("1"),
        )
    )
    db_session.commit()
    names = [h.name for h in db_session.query(Holding).all()]
    assert _ISOLATION_MARKER in names

    with get_engine().connect() as conn:
        outside = conn.execute(text("SELECT count(*) FROM holdings")).scalar_one()
    assert outside == 0


def test_sessionlocal_shares_db_session_transaction(db_session: Session) -> None:
    from app.core.database import SessionLocal

    other = SessionLocal()
    try:
        other.add(
            Holding(
                user_id=TEST_USER_ID,
                name="issue-27-sessionlocal",
                pricing_mode="auto",
                currency="USD",
                asset_class="STOCK",
                shares=Decimal("1"),
            )
        )
        other.commit()
        names = [h.name for h in db_session.query(Holding).all()]
        assert "issue-27-sessionlocal" in names
    finally:
        other.close()
