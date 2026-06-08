"""Celery tasks for the ADR-002 capture layer.

Credit-free (RSS + yfinance). Scheduled at market-session nodes by Beat (see
app/tasks/__init__.py). Catch-up lives here, not in Beat: capture_prices fetches
a multi-day OHLCV window and capture_news a 48h window, both upserted
idempotently, so a missed fire is covered by the next one within the fetch
horizon.
"""

from __future__ import annotations

import logging
from typing import Any

from app.tasks import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.capture_news_task",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def capture_news_task(self: Any) -> dict[str, int]:
    """Fetch recent RSS and upsert into the news table."""
    from app.core.database import SessionLocal
    from app.services.news_capture import capture_news

    session = SessionLocal()
    try:
        inserted = capture_news(session)
        return {"inserted": inserted}
    except Exception as exc:
        logger.exception("capture_news_task: failed, scheduling retry")
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.capture_prices_task",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def capture_prices_task(self: Any, market: str, session_node: str) -> dict[str, Any]:
    """Capture one (market, session_node) into price_snapshots."""
    from app.core.database import SessionLocal
    from app.services.price_capture import capture_prices

    session = SessionLocal()
    try:
        written = capture_prices(session, market, session_node)
        return {"market": market, "session_node": session_node, "written": written}
    except Exception as exc:
        logger.exception("capture_prices_task: failed for %s/%s", market, session_node)
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
