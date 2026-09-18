"""add vigil delivery events

Issue #457 (Vigil R0 P3.2): adds vigil_delivery_events, the signed and
correlated delivery-fact table. Field/constraint contract is #450 Design
section 4 (Appendix A "delivery_events" row), incorporated by reference.

`outbox_id` is a nullable FK: unmatched events (webhook-before-response,
shared-provider report mail, wrong address) are retained for later
association and never credited to a vault.

Revision ID: c8e4a1b7d902
Revises: 26050c5392cb
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c8e4a1b7d902"
down_revision: str | None = "26050c5392cb"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_VALID_SOURCES = ("webhook", "poll")


def upgrade() -> None:
    op.create_table(
        "vigil_delivery_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("provider_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_source", sa.Text(), nullable=False),
        sa.Column("address_cipher", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vigil_delivery_events")),
        sa.ForeignKeyConstraint(
            ["outbox_id"],
            ["vigil_outbox.id"],
            name=op.f("fk_vigil_delivery_events_outbox_id_vigil_outbox"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "provider_event_id", name=op.f("uq_vigil_delivery_events_provider_event_id")
        ),
        sa.CheckConstraint(
            "evidence_source IN (" + ", ".join(f"'{s}'" for s in _VALID_SOURCES) + ")",
            name=op.f("ck_vigil_delivery_events_evidence_source"),
        ),
    )


def downgrade() -> None:
    op.drop_table("vigil_delivery_events")
