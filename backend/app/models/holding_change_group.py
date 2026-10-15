"""Encrypted field-level history of user holdings writes."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.models.base import Base


class HoldingChangeGroup(Base):
    __tablename__ = "holding_change_groups"
    __table_args__ = (
        CheckConstraint("source IN ('web', 'file', 'agent')", name="source"),
        Index("ix_holding_change_groups_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    token_name: Mapped[str | None] = mapped_column(Text)
    token_prefix: Mapped[str | None] = mapped_column(Text)
    changes: Mapped[str] = mapped_column(EncryptedString, nullable=False)
