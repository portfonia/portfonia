"""Issue #675 migration backfill, cash CHECKs, and round trip."""

import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import get_settings


def test_referral_migration_backfill_and_round_trip(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "d64000000001")
    engine = create_engine(get_settings().database_url)
    uid = uuid.uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id,auth_provider,auth_subject,email,status,locale,base_currency,report_cadence) VALUES (:id,'supabase',:sub,'migration@example.com','active','en','USD','none')"
                ),
                {"id": uid, "sub": str(uid)},
            )
            conn.execute(
                text("INSERT INTO waitlist_entries (email,locale) VALUES ('wait@example.com','en')")
            )
        command.upgrade(alembic_cfg, "head")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT invited_by,grand_invited_by FROM users WHERE id=:id"), {"id": uid}
            ).one()
            assert tuple(row) == (uuid.UUID(get_settings().ADMIN_ID),) * 2
            assert (
                conn.scalar(
                    text("SELECT source FROM waitlist_entries WHERE email='wait@example.com'")
                )
                == "organic"
            )
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET credit_cash_balance=-0.50 WHERE id=:id"), {"id": uid}
            )
            conn.execute(
                text(
                    "INSERT INTO credit_ledger (user_id,bucket,amount,balance_after,reason,actor_type,idempotency_key) VALUES (:id,'cash',-0.50,-0.50,'referral_clawback','system','negative')"
                ),
                {"id": uid},
            )
        with pytest.raises(IntegrityError):
            command.downgrade(alembic_cfg, "d64000000001")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET credit_gift_balance=-0.01 WHERE id=:id"), {"id": uid}
            )
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM credit_ledger"))
            conn.execute(text("DELETE FROM waitlist_entries"))
            conn.execute(text("DELETE FROM users"))
        command.downgrade(alembic_cfg, "d64000000001")
        command.upgrade(alembic_cfg, "head")
    finally:
        engine.dispose()
