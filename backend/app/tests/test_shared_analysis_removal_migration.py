"""Issue #640 acceptance 5: drop the four shared-analysis tables; downgrade
recreates them empty."""

from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from app.core.config import get_settings

_TABLES = ("ticker_intel", "macro_event_intel", "cross_name_intel", "search_cache")


def _existing(engine_url: str) -> set[str]:
    engine = create_engine(engine_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                ),
                {"names": list(_TABLES)},
            )
            return {str(row[0]) for row in rows}
    finally:
        engine.dispose()


def test_01_drop_and_recreate_shared_analysis_tables(alembic_cfg: Config) -> None:
    url = get_settings().database_url
    command.upgrade(alembic_cfg, "d64400000001")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO search_cache (query_hash, query, trade_date, results) "
                    "VALUES ('h', 'q', '2026-10-01', '[]')"
                )
            )
        command.upgrade(alembic_cfg, "head")
        assert _existing(url) == set()

        command.downgrade(alembic_cfg, "d64400000001")
        assert _existing(url) == set(_TABLES)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM search_cache")).scalar_one() == 0
        command.upgrade(alembic_cfg, "head")
        assert _existing(url) == set()
    finally:
        engine.dispose()
