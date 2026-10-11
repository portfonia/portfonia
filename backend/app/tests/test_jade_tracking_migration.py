"""Issue #729 nullable JSONB migration and downgrade on real Postgres."""

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB

from alembic import command
from app.core.config import get_settings


def test_b2_migration(alembic_cfg: Config) -> None:
    script = ScriptDirectory.from_config(alembic_cfg)
    assert script.get_heads() == ["d72900000001"]
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(get_settings().database_url)
    try:
        c = next(c for c in inspect(engine).get_columns("users") if c["name"] == "jade_benchmark")
        assert c["nullable"] and isinstance(c["type"], JSONB)
        command.downgrade(alembic_cfg, "d72000000001")
        assert "jade_benchmark" not in {c["name"] for c in inspect(engine).get_columns("users")}
        command.upgrade(alembic_cfg, "head")
    finally:
        engine.dispose()
