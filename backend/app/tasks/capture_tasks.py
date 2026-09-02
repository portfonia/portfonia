"""Celery tasks for the ADR-002 capture layer.

Credit-free (RSS + yfinance). Scheduled at market-session nodes by Beat (see
app/tasks/__init__.py). Catch-up lives here, not in Beat: capture_prices fetches
a multi-day OHLCV window and capture_news a 48h window, both upserted
idempotently, so a missed fire is covered by the next one within the fetch
horizon.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from app.services.email_sender import send_ops_alert
from app.services.github_issues import create_bug_report, truncate_text
from app.tasks import celery_app

logger = logging.getLogger(__name__)

# str(exc) for a multi-row INSERT overflow includes the compiled SQL
# (thousands of chars). Unbounded interpolation 422s GitHub's issue-body
# limit; the full traceback stays in worker.log (issue #195).
_MAX_EXC_CHARS = 4_000
# Per-market slice so three failures still fit under _MAX_EXC_CHARS after
# join + the "RuntimeError: " prefix _format_exc adds.
_MAX_MARKET_EXC_CHARS = 1_200
_EXC_TRUNCATION_MARK = "...(truncated)"
# SQLAlchemy/psycopg interpolates bound parameters into str(exc). Those
# bindings are holdings-derived identifiers (ticker / fund_code). Application
# logs omit them (Concept §8.8); ops email + auto GitHub issues used not to.
_SQL_PARAMETERS_RE = re.compile(r"\[parameters:.*?\]", re.DOTALL)


def _scrub_sql_parameters(text: str) -> str:
    return _SQL_PARAMETERS_RE.sub("[parameters: redacted]", text)


def _format_exc(exc: BaseException) -> str:
    return truncate_text(
        _scrub_sql_parameters(f"{type(exc).__name__}: {exc}"),
        _MAX_EXC_CHARS,
        mark=_EXC_TRUNCATION_MARK,
    )


def _market_failure_entry(market: str, exc: BaseException) -> str:
    detail = truncate_text(
        _scrub_sql_parameters(f"{type(exc).__name__}: {exc}"),
        _MAX_MARKET_EXC_CHARS,
        mark=_EXC_TRUNCATION_MARK,
    )
    return f"{market}: {detail}"


def _capture_failed(task_name: str, exc: BaseException, context: str = "") -> None:
    """Send ops alert + create GitHub issue when a capture task exhausts retries."""
    formatted = _format_exc(exc)
    detail = f"{context}\n\nerror: {formatted}" if context else f"error: {formatted}"
    send_ops_alert(
        subject=f"[Portfonia] capture FAILED — {task_name}",
        body=(
            f"{task_name} exhausted all retries.\n\n"
            f"{detail}\n\n"
            f"Impact: data missing from next report window.\n"
            f"Check worker.log for the full traceback."
        ),
    )
    create_bug_report(
        title=f"capture failure: {task_name}",
        body=(
            f"## Capture task exhausted retries\n\n"
            f"**Task:** `{task_name}`\n\n"
            f"**Error:** `{formatted}`\n\n"
            f"{'**Context:** ' + context + chr(10) + chr(10) if context else ''}"
            f"**Impact:** data for this capture node will be missing from the next "
            f"report window, potentially causing stale prices, missing news, or "
            f"incomplete portfolio valuation.\n\n"
            f"**Investigate:** check `worker.log` for the full traceback."
        ),
        labels=["bug", "ops", "capture"],
    )


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
        if self.request.retries >= self.max_retries:
            _capture_failed("capture_news_task", exc)
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
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "capture_prices_task", exc, context=f"market={market} session_node={session_node}"
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.backfill_ohlcv_task",
    bind=True,
    max_retries=1,
    default_retry_delay=60,
)
def backfill_ohlcv_task(self: Any, tickers: list[str] | None = None) -> dict[str, Any]:
    """Backfill ~1 year of OHLCV closes for the given tickers.

    Dispatched by confirm_holdings with that user's sparse auto-priced tickers
    (< 50 close bars). Daily capture stays on capture_prices_task (full market
    universe, 7-day lookback). The ops script backfill_ohlcv.py remains the
    one-shot full-universe seed. Idempotent on (ticker, market, session_node,
    trade_date).
    """
    from app.core.database import SessionLocal
    from app.services.price_capture import capture_prices

    if not tickers:
        logger.info("backfill_ohlcv_task: no tickers requested")
        return {"written": 0}

    _LOOKBACK_DAYS = 420
    from app.services.markets import CAPTURE_MARKET_ORDER

    _MARKETS = CAPTURE_MARKET_ORDER
    session = SessionLocal()
    try:
        total = 0
        failures: list[tuple[str, BaseException]] = []
        # Full-market retry is deliberate: upsert is idempotent, and tracking
        # which markets succeeded across Celery retries needs task state we
        # do not have. Failures should be rare after the chunked-upsert fix.
        for market in _MARKETS:
            try:
                written = capture_prices(
                    session,
                    market,
                    "close",
                    lookback_days=_LOOKBACK_DAYS,
                    tickers=tickers,
                )
                logger.info("backfill_ohlcv_task: %s: %d bars upserted", market, written)
                total += written
            except Exception as exc:
                logger.exception("backfill_ohlcv_task: %s failed", market)
                failures.append((market, exc))
                session.rollback()
        if failures:
            summary = "; ".join(_market_failure_entry(m, e) for m, e in failures)
            combined = RuntimeError(summary)
            if self.request.retries >= self.max_retries:
                _capture_failed("backfill_ohlcv_task", combined)
            raise self.retry(exc=combined) from combined
        logger.info("backfill_ohlcv_task: complete — %d bars total", total)
        return {"written": total}
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
    POST /admin/portfolio/refresh entry point (then at POST /portfolio/refresh,
    before the ops-token split — issue #128 checkpoint B2), so rates went
    stale whenever no one
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
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "capture_fx_task",
                exc,
                context="FX rates will be stale in the next report — portfolio CNY/HKD values and the FX-stale warning will both be affected.",
            )
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
    """Fetch settled NAV history from Tiantian Fund for fund_code holdings into price_snapshots."""
    from app.core.database import SessionLocal
    from app.services.price_capture import capture_fund_navs

    session = SessionLocal()
    try:
        written = capture_fund_navs(session)
        return {"written": written}
    except Exception as exc:
        logger.exception("capture_fund_navs_task: failed")
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "capture_fund_navs_task",
                exc,
                context="Fund NAV data (019547/008142/110011) will be missing — these holdings will be excluded from portfolio valuation.",
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.backfill_fund_navs_task",
    bind=True,
    max_retries=1,
    default_retry_delay=60,
)
def backfill_fund_navs_task(self: Any, fund_codes: list[str] | None = None) -> dict[str, Any]:
    """Fetch settled NAV history for the given fund_codes.

    Dispatched by confirm_holdings for this user's auto-priced funds that have
    no close in price_snapshots. Daily capture stays on capture_fund_navs_task
    (full fund universe, same 30-day lookback). Idempotent on
    (ticker, market, session_node, trade_date). Funds are not a §4.4 series
    (compute_technical_positions skips no-ticker holdings), so this is a
    valuation/anomaly cold-start, not a 420-day OHLCV seed.
    """
    from app.core.database import SessionLocal
    from app.services.price_capture import capture_fund_navs

    if not fund_codes:
        logger.info("backfill_fund_navs_task: no fund_codes requested")
        return {"written": 0}

    _LOOKBACK_DAYS = 30
    session = SessionLocal()
    try:
        written = capture_fund_navs(session, lookback_days=_LOOKBACK_DAYS, fund_codes=fund_codes)
        if written == 0:
            # fetch_nav_history swallows HTTP/parse errors as []. A total miss
            # here is the #196 incident with no signal: the daily beat will
            # retry tomorrow, but this one-shot would otherwise SUCCESS.
            logger.warning(
                "backfill_fund_navs_task: wrote 0 bars for %d requested fund_code(s)",
                len(fund_codes),
            )
            raise RuntimeError(
                f"NAV capture wrote 0 bars for {len(fund_codes)} requested fund_code(s)"
            )
        logger.info("backfill_fund_navs_task: complete — %d bars total", written)
        return {"written": written}
    except Exception as exc:
        logger.exception("backfill_fund_navs_task: failed")
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "backfill_fund_navs_task",
                exc,
                context="Newly confirmed fund_code holdings will have no NAV until the next scheduled capture.",
            )
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
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "capture_forward_events_task",
                exc,
                context="§2.5 forward calendar will be empty or incomplete in the next report.",
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
