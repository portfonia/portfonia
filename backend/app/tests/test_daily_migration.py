"""Issue #650 acceptance 1: Daily CHECK expansion on real Postgres."""

import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import get_settings


def test_daily_acceptance_1_migration(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(get_settings().database_url)
    insert = text(
        "INSERT INTO users (id, auth_provider, email, status, locale, base_currency, "
        "report_cadence, subscription_type) VALUES "
        "(:id, 'supabase', :email, 'active', 'en', 'USD', 'daily', :plan)"
    )
    uid = uuid.uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(insert, {"id": uid, "email": f"{uid}@example.com", "plan": "daily"})
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT report_cadence, subscription_type FROM users WHERE id=:id"),
                {"id": uid},
            ).one() == ("daily", "daily")
        with (
            pytest.raises(IntegrityError, match="ck_users_subscription_type"),
            engine.begin() as conn,
        ):
            conn.execute(
                insert, {"id": uuid.uuid4(), "email": "monthly@example.com", "plan": "monthly"}
            )
        with pytest.raises(IntegrityError):
            command.downgrade(alembic_cfg, "d62200000001")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id=:id"), {"id": uid})
        command.downgrade(alembic_cfg, "d62200000001")
        command.upgrade(alembic_cfg, "head")
    finally:
        engine.dispose()
