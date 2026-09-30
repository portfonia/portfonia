"""Preserve legacy cadences through the subscription schema migration."""

import uuid

from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from app.core.config import get_settings


def test_subscription_migration_preserves_existing_cadences(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "d58200000001")
    engine = create_engine(get_settings().database_url)
    ids = [uuid.uuid4(), uuid.uuid4()]
    try:
        with engine.begin() as conn:
            for uid, cadence in zip(ids, ("weekly", "mwf"), strict=True):
                conn.execute(
                    text(
                        "INSERT INTO users (id, auth_provider, email, status, locale, base_currency, report_cadence) VALUES (:id, 'supabase', :email, 'active', 'en', 'USD', :cadence)"
                    ),
                    {"id": uid, "email": f"{uid}@example.com", "cadence": cadence},
                )
        command.upgrade(alembic_cfg, "s59500000001")
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT report_cadence, subscription_status, subscription_type FROM users WHERE id IN (:a,:b) ORDER BY report_cadence"
                ),
                {"a": ids[0], "b": ids[1]},
            ).all()
            assert [tuple(r) for r in rows] == [
                ("mwf", "inactive", None),
                ("weekly", "inactive", None),
            ]
        command.downgrade(alembic_cfg, "d58200000001")
        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT report_cadence FROM users WHERE id IN (:a,:b) ORDER BY report_cadence"
                ),
                {"a": ids[0], "b": ids[1]},
            ).scalars().all() == ["mwf", "weekly"]
        command.upgrade(alembic_cfg, "s59500000001")
    finally:
        engine.dispose()
