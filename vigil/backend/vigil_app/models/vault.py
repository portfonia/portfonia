from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from vigil_app.models.base import Base

VAULT_PHASES = (
    "ARMED",
    "CHALLENGE_1",
    "CHALLENGE_2",
    "DISARMED",
    "FINAL_WARNING",
    "RELEASED",
    "REVOKED",
)


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(values))
    return f"{column} IN ({quoted})"


class Vault(Base):
    __tablename__ = "vaults"
    __table_args__ = (
        CheckConstraint(_in_list_sql("phase", VAULT_PHASES), name="phase"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    owner_auth_subject: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    active_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    active_object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    next_check_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    hold_reason: Mapped[str | None] = mapped_column(Text)
    held_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_owner_confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    retention_anchor_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    first_armed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
