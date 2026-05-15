"""Walks every Alembic revision through upgrade -> downgrade -> upgrade.

If any migration's downgrade is missing, asymmetric, or breaks the schema
in a way that prevents re-upgrade, this test fails. Guards against the
classic Alembic failure mode of an unverified downgrade path silently
rotting until rollback is needed in production.
"""

from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command


def test_migrations_round_trip(alembic_cfg: Config) -> None:
    script = ScriptDirectory.from_config(alembic_cfg)
    revisions = list(script.walk_revisions())[::-1]  # oldest -> newest

    assert revisions, "no migrations discovered under alembic/versions/"

    for rev in revisions:
        command.upgrade(alembic_cfg, rev.revision)
        command.downgrade(alembic_cfg, "-1")
        command.upgrade(alembic_cfg, rev.revision)
