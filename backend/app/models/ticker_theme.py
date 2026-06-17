from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TickerTheme(Base):
    __tablename__ = "ticker_themes"

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    theme: Mapped[str] = mapped_column(Text, nullable=False)
    theme_label_zh: Mapped[str] = mapped_column(Text, nullable=False)
    theme_label_en: Mapped[str] = mapped_column(Text, nullable=False)
    asset_class: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
