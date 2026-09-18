"""add vigil cycles and rounds

Issue #459 (Vigil R0 P3.3): vigil_cycles / vigil_rounds plus a real FK
from vigil_action_tokens.cycle_id. Field/constraint contract is #450
Design section 4 (cycles / rounds rows), incorporated by reference.

Revision ID: a9c3e7f1b204
Revises: e7a1c4d9b218
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a9c3e7f1b204"
down_revision: str | None = "e7a1c4d9b218"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_VALID_STATUSES = ("active", "confirmed", "released", "cancelled")


def upgrade() -> None:
    op.create_table(
        "vigil_cycles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_level", sa.BigInteger(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_cycles")),
        sa.ForeignKeyConstraint(
            ["vault_id"],
            ["vigil_vaults.id"],
            name=op.f("fk_vigil_cycles_vault_id_vigil_vaults"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["vigil_configurations.id"],
            name=op.f("fk_vigil_cycles_config_id_vigil_configurations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["vigil_objects.id"],
            name=op.f("fk_vigil_cycles_object_id_vigil_objects"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _VALID_STATUSES) + ")",
            name=op.f("ck_vigil_cycles_status"),
        ),
        sa.CheckConstraint(
            "current_level >= 1 AND current_level <= 3",
            name=op.f("ck_vigil_cycles_current_level_bounds"),
        ),
    )
    op.create_index(
        "uq_vigil_cycles_one_active",
        "vigil_cycles",
        ["vault_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "vigil_rounds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("level", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_rounds")),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["vigil_cycles.id"],
            name=op.f("fk_vigil_rounds_cycle_id_vigil_cycles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_id"],
            ["vigil_outbox.id"],
            name=op.f("fk_vigil_rounds_outbox_id_vigil_outbox"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "cycle_id",
            "level",
            "generation",
            name=op.f("uq_vigil_rounds_cycle_id_level_generation"),
        ),
        sa.CheckConstraint(
            "level >= 1 AND level <= 3",
            name=op.f("ck_vigil_rounds_level_bounds"),
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name=op.f("ck_vigil_rounds_generation_nonneg"),
        ),
        sa.CheckConstraint(
            "(anchor_at IS NULL AND deadline_at IS NULL) "
            "OR (anchor_at IS NOT NULL AND deadline_at IS NOT NULL)",
            name=op.f("ck_vigil_rounds_anchor_deadline_pair"),
        ),
    )
    op.create_index(
        "uq_vigil_rounds_one_open_generation",
        "vigil_rounds",
        ["cycle_id", "level"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_foreign_key(
        op.f("fk_vigil_action_tokens_cycle_id_vigil_cycles"),
        "vigil_action_tokens",
        "vigil_cycles",
        ["cycle_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_vigil_action_tokens_cycle_id_vigil_cycles"),
        "vigil_action_tokens",
        type_="foreignkey",
    )
    op.drop_index("uq_vigil_rounds_one_open_generation", table_name="vigil_rounds")
    op.drop_table("vigil_rounds")
    op.drop_index("uq_vigil_cycles_one_active", table_name="vigil_cycles")
    op.drop_table("vigil_cycles")
