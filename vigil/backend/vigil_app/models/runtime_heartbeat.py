from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from vigil_app.models.base import Base

HEARTBEAT_HEALTH = ("held", "ok")


class RuntimeHeartbeat(Base):
    __tablename__ = "runtime_heartbeat"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(
            "health IN (" + ", ".join(f"'{v}'" for v in sorted(HEARTBEAT_HEALTH)) + ")",
            name="health",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_scan_completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_dependency_check_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    health: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'held'"))
    reason: Mapped[str | None] = mapped_column(Text)
