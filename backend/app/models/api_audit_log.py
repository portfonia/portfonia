"""Agent request metadata, without holding values or plaintext credentials."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, SmallInteger, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ApiAuditLog(Base):
    __tablename__ = "api_audit_log"
    __table_args__ = (Index("ix_api_audit_log_user_occurred", "user_id", "occurred_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_tokens.id")
    )
    token_prefix: Mapped[str | None] = mapped_column(Text)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    item_count: Mapped[int | None] = mapped_column(Integer)
    client_ip: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
