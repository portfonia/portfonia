"""SQLAlchemy engine + session factory. Sync, single-user (Ring 0)."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a DB session and ensures it closes."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
