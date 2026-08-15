"""Celery beat cleanup for the L1 shared-intel caches (issue #128 A2 —
design doc §4.4, Hermes/Portfonia/Docs/Ring 1-A design.md).

`ticker_intel`/`search_cache` grow one row per (identifier|query, trade_date)
per day now that every active user's fan-out shares them — unlike the Ring 0
`upload_job` "known accepted gap: no retention" (harmless at 1 user), an
unbounded multi-user + full-identifier-universe write pattern would grow
these tables without bound. 90 days is generous relative to the tables'
actual value: once a fresher trade_date's row exists for the same
identifier/query, an old row carries no independent use (it is never read
back — every lookup is keyed on the CURRENT trade_date).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.tasks import celery_app

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 90


def _cleanup_expired(session: Session, cutoff: date) -> dict[str, int]:
    """Delete every ticker_intel/search_cache row with trade_date < cutoff.
    Commits and returns the per-table delete counts. Split out from the
    Celery task itself so it's testable against a real db_session without
    mocking SessionLocal (issue #128 A2)."""
    from app.models.search_cache import SearchCache
    from app.models.ticker_intel import TickerIntel

    ti_result = cast(
        CursorResult[Any],
        session.execute(delete(TickerIntel).where(TickerIntel.trade_date < cutoff)),
    )
    sc_result = cast(
        CursorResult[Any],
        session.execute(delete(SearchCache).where(SearchCache.trade_date < cutoff)),
    )
    session.commit()
    return {
        "ticker_intel_deleted": ti_result.rowcount or 0,
        "search_cache_deleted": sc_result.rowcount or 0,
    }


@celery_app.task(name="app.tasks.cache_tasks.sweep_stale_shared_intel_cache")  # type: ignore[untyped-decorator]
def sweep_stale_shared_intel_cache() -> dict[str, int]:
    """Daily backstop: delete ticker_intel/search_cache rows older than
    `_RETENTION_DAYS`. See module docstring for why this table needs a
    retention sweep where upload_job's "no cleanup" gap does not."""
    from app.core.database import SessionLocal

    cutoff = (datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)).date()
    session = SessionLocal()
    try:
        result = _cleanup_expired(session, cutoff)
        logger.info(
            "sweep_stale_shared_intel_cache: deleted %d ticker_intel, %d search_cache "
            "row(s) older than %s",
            result["ticker_intel_deleted"],
            result["search_cache_deleted"],
            cutoff,
        )
        return result
    finally:
        session.close()
