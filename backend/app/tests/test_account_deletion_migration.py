"""Issue #644 acceptance 1: ledger CHECKs and downgrade."""

from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import get_settings


def test_01_relinquish_migration(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "d64200000001")
    engine = create_engine(get_settings().database_url)
    insert = text(
        "INSERT INTO credit_ledger (user_id,bucket,amount,balance_after,reason,actor_type,idempotency_key) VALUES (:uid,'cash',-12,0,:reason,:actor,:key)"
    )
    params = {"uid": uuid4(), "reason": "relinquish", "actor": "user", "key": "fixture"}
    try:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, params)
        command.upgrade(alembic_cfg, "head")
        with engine.connect() as conn:
            constraints: dict[str, str] = {
                str(row[0]): str(row[1])
                for row in conn.execute(
                    text(
                        "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid='credit_ledger'::regclass AND contype='c'"
                    )
                )
            }
        assert "relinquish" in constraints["ck_credit_ledger_reason"]
        assert "user" in constraints["ck_credit_ledger_actor_type"]
        with engine.begin() as conn:
            conn.execute(insert, params)
        with pytest.raises(IntegrityError):
            command.downgrade(alembic_cfg, "d64200000001")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM credit_ledger WHERE idempotency_key='fixture'"))
        command.downgrade(alembic_cfg, "d64200000001")
        for reason, actor in [("relinquish", "system"), ("refund", "user")]:
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(insert, {**params, "reason": reason, "actor": actor})
        command.upgrade(alembic_cfg, "head")
    finally:
        engine.dispose()
