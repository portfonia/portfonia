"""Issue #842 CHECK constraint renames and downgrade on real Postgres."""

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from app.core.config import get_settings


def test_leverage_constraint_rename_migration(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(get_settings().database_url)
    try:
        assert {
            c["name"] for c in inspect(engine).get_check_constraints("ticker_leverage_overrides")
        } == {
            "ck_ticker_leverage_overrides_asset_class",
            "ck_ticker_leverage_overrides_direction",
            "ck_ticker_leverage_overrides_leverage_multiple_positive",
        }
        command.downgrade(alembic_cfg, "k83900000001")
        assert {
            c["name"] for c in inspect(engine).get_check_constraints("ticker_leverage_overrides")
        } == {
            "ck_ticker_leverage_overrides_asset_class",
            "ck_ticker_leverage_overrides_ck_ticker_leverage_overrid_4d1b",
            "ck_ticker_leverage_overrides_ck_ticker_leverage_overrid_28ef",
        }
    finally:
        try:
            command.upgrade(alembic_cfg, "head")
        finally:
            engine.dispose()
