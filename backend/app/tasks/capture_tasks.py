"""Celery tasks for the ADR-002 capture layer.

Credit-free (RSS + yfinance). Scheduled at market-session nodes by Beat (see
app/tasks/__init__.py). Catch-up lives here, not in Beat: capture_prices fetches
a multi-day OHLCV window and capture_news a 48h window, both upserted
idempotently, so a missed fire is covered by the next one within the fetch
horizon.
"""

from __future__ import annotations

import logging
from datetime import datetime
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


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.capture_fx_task",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def capture_fx_task(self: Any) -> dict[str, Any]:
    """Fetch today's FX rates and upsert into fx_rates.

    Until this task existed, FX was only refreshed by the manual
    POST /portfolio/refresh entry point, so rates went stale whenever no one
    triggered it (observed: rates frozen at 2026-06-04 while reports ran on
    06-10). The upsert is idempotent, so a missed fire is covered by the next
    daily run. (R-4)
    """
    from app.core.database import SessionLocal
    from app.services.fx_fetcher import update_fx_rates

    session = SessionLocal()
    try:
        result = update_fx_rates(session)
        session.commit()
        return {"upserted": result.upserted, "failed": result.failed}
    except Exception as exc:
        session.rollback()
        logger.exception("capture_fx_task: failed, scheduling retry")
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


# Capture a bit wider than the report's forward window so a missed daily fire is
# still covered by the next one (catch-up in the task, no watermark — same pattern
# as prices/news).
@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.capture_fund_navs_task",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def capture_fund_navs_task(self: Any) -> dict[str, int]:
    """Fetch settled NAV history from 天天基金 for fund_code holdings into price_snapshots."""
    from app.core.database import SessionLocal
    from app.services.price_capture import capture_fund_navs

    session = SessionLocal()
    try:
        written = capture_fund_navs(session)
        return {"written": written}
    except Exception as exc:
        logger.exception("capture_fund_navs_task: failed")
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


_FORWARD_HORIZON_DAYS = 14


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.capture_forward_events_task",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def capture_forward_events_task(self: Any) -> dict[str, int]:
    """Capture US forward events (FRED macro + FOMC + held-company earnings)."""
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.core.timezones import ET
    from app.services.forward_events import (
        ForwardEventData,
        fetch_earnings_dates,
        fetch_fomc_dates,
        fetch_fred_release_dates,
        persist_forward_events,
    )
    from app.services.price_capture import _market_tickers

    settings = get_settings()
    today = datetime.now(tz=ET).date()
    session = SessionLocal()
    try:
        events: list[ForwardEventData] = []
        if settings.FRED_API_KEY is not None:
            events += fetch_fred_release_dates(
                settings.FRED_API_KEY.get_secret_value(), today, _FORWARD_HORIZON_DAYS
            )
        else:
            logger.warning(
                "capture_forward_events_task: FRED_API_KEY unset — skipping macro releases"
            )
        events += fetch_fomc_dates(today, _FORWARD_HORIZON_DAYS)
        events += fetch_earnings_dates(_market_tickers(session, "US"), today, _FORWARD_HORIZON_DAYS)
        captured = persist_forward_events(session, events)
        return {"captured": captured}
    except Exception as exc:
        logger.exception("capture_forward_events_task: failed")
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
