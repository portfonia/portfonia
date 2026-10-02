"""Irreversible data conversion and reversible schema on a local test DB."""

from datetime import UTC, datetime, timedelta

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.core.config import get_settings


def test_acceptance_02_migration_and_downgrade(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "s59500000001")
    engine = create_engine(get_settings().database_url)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for age in [40, 3]:
            conn.execute(
                text(
                    "INSERT INTO news(url_hash,title,summary,source,url,published_at,fetched_at) VALUES (:hash,'Old headline','Old summary','FixturePublisher','https://fixture.example',:published,:fetched)"
                ),
                {"hash": str(age), "published": now - timedelta(days=age), "fetched": now},
            )
    command.upgrade(alembic_cfg, "d62000000001")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM news")).mappings().one()
        assert row["url_hash"] == "3" and row["origin"] == "pool" and row["kind"] == "article"
        record = row["record"]
        assert set(record) == {
            "v",
            "kind",
            "title",
            "summary",
            "published_at",
            "collected_at",
            "label",
            "filing_form",
        }
        assert (
            record["title"] == "Old headline"
            and record["summary"] == "Old summary"
            and record["label"] is None
        )
        assert datetime.fromisoformat(record["published_at"]) == now - timedelta(days=3)
        assert datetime.fromisoformat(record["collected_at"]) == now
        assert not {"url", "source", "title", "summary"} & {
            c["name"] for c in inspect(conn).get_columns("news")
        }
    command.downgrade(alembic_cfg, "s59500000001")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM news")).mappings().one()
        assert row["title"] == "Old headline" and row["summary"] == "Old summary"
        assert row["url"] == row["source"] == ""
    engine.dispose()
