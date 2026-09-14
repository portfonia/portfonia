"""P1.1-A01: Vigil upgrade creates only Vigil tables; Portfonia sentinel stays put."""

from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from alembic import command
from vigil_app.core.config import get_settings
from vigil_app.core.database import reset_engine
from vigil_app.tests.conftest import SENTINEL_DB_NAME

_VIGIL_TABLES = frozenset(
    {
        "alembic_version",
        "vaults",
        "audit_events",
        "runtime_heartbeat",
        "consumed_nonces",
    }
)


def test_exactly_one_alembic_head(alembic_cfg: Config) -> None:
    heads = ScriptDirectory.from_config(alembic_cfg).get_heads()
    assert len(heads) == 1, heads


def test_migrations_round_trip(alembic_cfg: Config) -> None:
    script = ScriptDirectory.from_config(alembic_cfg)
    revisions = list(script.walk_revisions())[::-1]
    assert revisions
    for rev in revisions:
        command.upgrade(alembic_cfg, rev.revision)
        command.downgrade(alembic_cfg, "-1")
        command.upgrade(alembic_cfg, rev.revision)


def test_upgrade_creates_only_vigil_tables_and_leaves_portfonia_sentinel(
    alembic_cfg: Config, sentinel_db: Engine
) -> None:
    command.upgrade(alembic_cfg, "head")

    vigil_engine = create_engine(get_settings().database_url)
    try:
        vigil_tables = set(inspect(vigil_engine).get_table_names())
    finally:
        vigil_engine.dispose()

    assert vigil_tables == _VIGIL_TABLES

    inspector = inspect(sentinel_db)
    assert set(inspector.get_table_names()) == {"holdings", "alembic_version"}
    with sentinel_db.connect() as conn:
        note = conn.execute(text("SELECT note FROM holdings WHERE id = 1")).scalar_one()
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert note == "sentinel-row"
    assert revision == "13144ce0sent1"
    assert SENTINEL_DB_NAME not in get_settings().database_url


def test_vaults_has_nullable_active_ids_without_fk(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "head")
    engine = create_engine(get_settings().database_url)
    try:
        inspector = inspect(engine)
        columns = {col["name"]: col for col in inspector.get_columns("vaults")}
        assert columns["active_config_id"]["nullable"] is True
        assert columns["active_object_id"]["nullable"] is True
        fks = inspector.get_foreign_keys("vaults")
        referred = {tuple(fk.get("constrained_columns") or ()) for fk in fks}
        assert ("active_config_id",) not in referred
        assert ("active_object_id",) not in referred
    finally:
        engine.dispose()
        reset_engine()
