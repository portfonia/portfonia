"""Celery beat cleanup for the shared-intel caches (issue #128 A2/A3 —
design doc §4.4/§5.4, Hermes/Portfonia/Docs/Ring 1-A design.md).

`ticker_intel`/`search_cache`/`macro_event_intel` grow one row per
(identifier|query|event_key, trade_date) per day now that every active
user's fan-out shares them — unlike the Ring 0 `upload_job` "known accepted
gap: no retention" (harmless at 1 user), an unbounded multi-user +
full-identifier-universe write pattern would grow these tables without
bound. 90 days is generous relative to the tables' actual value: once a
fresher trade_date's row exists for the same identifier/query/event, an old
row carries no independent use (it is never read back — every lookup is
keyed on the CURRENT trade_date).

A3 extends this task rather than adding a second sweep: the retention rule,
the cutoff and the failure/alert path are identical, and a separate task
would be a second place for the same 90 to drift.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.services.email_sender import send_ops_alert
from app.tasks import celery_app

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 90


def _cleanup_expired(session: Session, cutoff: date) -> dict[str, int]:
    """Delete every ticker_intel/search_cache/macro_event_intel row with
    trade_date < cutoff. Commits and returns the per-table delete counts.
    Split out from the Celery task itself so it's testable against a real
    db_session without mocking SessionLocal (issue #128 A2)."""
    from app.models.macro_event_intel import MacroEventIntel
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
    mei_result = cast(
        CursorResult[Any],
        session.execute(delete(MacroEventIntel).where(MacroEventIntel.trade_date < cutoff)),
    )
    session.commit()
    return {
        "ticker_intel_deleted": ti_result.rowcount or 0,
        "search_cache_deleted": sc_result.rowcount or 0,
        "macro_event_intel_deleted": mei_result.rowcount or 0,
    }


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.cache_tasks.sweep_stale_shared_intel_cache",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def sweep_stale_shared_intel_cache(self: Any) -> dict[str, int]:
    """Daily backstop: delete ticker_intel/search_cache rows older than
    `_RETENTION_DAYS`. See module docstring for why this table needs a
    retention sweep where upload_job's "no cleanup" gap does not.

    Retry + ops-alert on exhaustion (round 2 review finding): unlike the
    other beat tasks in this codebase (backup, capture), this previously
    had no error handling at all — a silent sweep outage let both cache
    tables grow without bound, exactly the failure mode this task exists
    to prevent. Matches backup_database_task's pattern.
    """
    from app.core.database import SessionLocal

    cutoff = (datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)).date()
    session = SessionLocal()
    try:
        result = _cleanup_expired(session, cutoff)
        logger.info(
            "sweep_stale_shared_intel_cache: deleted %d ticker_intel, %d search_cache, "
            "%d macro_event_intel row(s) older than %s",
            result["ticker_intel_deleted"],
            result["search_cache_deleted"],
            result["macro_event_intel_deleted"],
            cutoff,
        )
        return result
    except Exception as exc:
        logger.exception("sweep_stale_shared_intel_cache: failed, scheduling retry")
        if self.request.retries >= self.max_retries:
            send_ops_alert(
                subject="[Portfonia] shared-intel cache sweep FAILED — retries exhausted",
                body=(
                    f"sweep_stale_shared_intel_cache failed after {self.max_retries} retries.\n\n"
                    f"error: {type(exc).__name__}: {exc}\n\n"
                    f"Impact: ticker_intel/search_cache/macro_event_intel rows older than "
                    f"{_RETENTION_DAYS} days "
                    f"are not being cleaned up — all three tables will keep growing until this is "
                    f"fixed. Not a correctness issue for reports, but check disk usage if this "
                    f"persists. Check worker.log for the full traceback."
                ),
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
