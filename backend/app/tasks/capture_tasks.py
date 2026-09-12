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
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from celery.exceptions import Retry  # type: ignore[import-untyped]

from app.services.capture_results import CaptureDataMiss, CaptureOutcome
from app.services.china_session_calendar import ChinaSessionWindow
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


def _window_from_context(
    ctx: dict[str, Any], lookback_days: int, max_lag: int
) -> ChinaSessionWindow:
    as_of = datetime.fromisoformat(str(ctx["as_of_utc"]))
    return ChinaSessionWindow(
        as_of_utc=as_of,
        window_start=date.fromisoformat(str(ctx["window_start"])),
        window_end=date.fromisoformat(str(ctx["window_end"])),
        latest_session=(
            date.fromisoformat(str(ctx["latest_session"])) if ctx.get("latest_session") else None
        ),
        cutoff=date.fromisoformat(str(ctx["cutoff"])) if ctx.get("cutoff") else None,
        calendar_status="ok" if ctx.get("calendar_status") == "ok" else "calendar_unknown",
    )


def _dump_window(window: ChinaSessionWindow) -> dict[str, Any]:
    return {
        "v": 1,
        "as_of_utc": window.as_of_utc.isoformat(),
        "window_start": window.window_start.isoformat(),
        "window_end": window.window_end.isoformat(),
        "latest_session": window.latest_session.isoformat() if window.latest_session else None,
        "cutoff": window.cutoff.isoformat() if window.cutoff else None,
        "calendar_status": window.calendar_status,
    }


def _load_nav_retry(
    retry_context: dict[str, Any] | None,
    lookback_days: int,
    max_lag: int,
    default_fund_codes: list[str] | None = None,
) -> tuple[ChinaSessionWindow, tuple[str, ...] | None, int, list[str] | None]:
    from app.services.china_session_calendar import freeze_capture_window

    if not retry_context:
        window = freeze_capture_window(datetime.now(tz=UTC), lookback_days, max_lag)
        return window, None, 0, default_fund_codes
    window = _window_from_context(retry_context, lookback_days, max_lag)
    unresolved = retry_context.get("unresolved") or []
    only_keys = tuple(str(k) for k in unresolved) if unresolved else None
    written = int(retry_context.get("written") or 0)
    codes = retry_context.get("fund_codes")
    fund_codes = [str(c) for c in codes] if isinstance(codes, list) else default_fund_codes
    return window, only_keys, written, fund_codes


def _finish_nav_task(
    task: Any,
    outcome: CaptureOutcome,
    window: ChinaSessionWindow,
    prior_written: int,
    fund_codes: list[str] | None,
    lookback_days: int,
    max_lag: int,
    task_name: str,
) -> dict[str, Any]:
    from app.services.price_capture import emit_nav_terminal_diagnostics

    total_written = prior_written + outcome.written
    if outcome.unresolved and task.request.retries < task.max_retries:
        raise task.retry(
            kwargs={
                "retry_context": {
                    **_dump_window(window),
                    "lookback_days": lookback_days,
                    "fund_codes": fund_codes,
                    "written": total_written,
                    "unresolved": [t.key for t in outcome.unresolved],
                }
            },
            countdown=task.default_retry_delay,
        )
    if outcome.unresolved:
        emit_nav_terminal_diagnostics(
            outcome.unresolved, as_of_date=window.window_end, max_lag_sessions=max_lag
        )
        raise CaptureDataMiss(
            f"{task_name} exhausted retry/fallback with unresolved NAV keys",
            CaptureOutcome(
                written=total_written,
                unresolved=outcome.unresolved,
                recovered=outcome.recovered,
                history_coverage=outcome.history_coverage,
            ),
        )
    logger.info("%s: complete written=%d", task_name, total_written)
    return {
        "written": total_written,
        "recovered": list(outcome.recovered),
        "unresolved": [],
        "history_coverage": outcome.history_coverage,
    }


def _load_price_retry(
    retry_context: dict[str, Any] | None,
    lookback_days: int,
    max_lag: int,
) -> tuple[ChinaSessionWindow, tuple[str, ...] | None, int, dict[str, dict[date, Any]]]:
    from app.services._tencent import OhlcBar
    from app.services.china_session_calendar import freeze_capture_window

    if not retry_context:
        window = freeze_capture_window(datetime.now(tz=UTC), lookback_days, max_lag)
        return window, None, 0, {}
    window = _window_from_context(retry_context, lookback_days, max_lag)
    unresolved = retry_context.get("unresolved") or []
    only_tickers = tuple(str(k) for k in unresolved) if unresolved else None
    written = int(retry_context.get("written") or 0)
    anchors: dict[str, dict[date, OhlcBar]] = {}
    raw_anchors = retry_context.get("yahoo_ohlc") or {}
    if isinstance(raw_anchors, dict):
        for ticker, by_day in raw_anchors.items():
            if not isinstance(by_day, dict):
                continue
            bars: dict[date, OhlcBar] = {}
            for day_s, parts in by_day.items():
                if not isinstance(parts, list) or len(parts) < 4:
                    continue
                day = date.fromisoformat(str(day_s))
                bars[day] = OhlcBar(
                    day,
                    Decimal(str(parts[0])),
                    Decimal(str(parts[1])),
                    Decimal(str(parts[2])),
                    Decimal(str(parts[3])),
                )
            if bars:
                anchors[str(ticker)] = bars
    return window, only_tickers, written, anchors


def _dump_price_retry(
    window: ChinaSessionWindow,
    outcome: CaptureOutcome,
    written: int,
    retained: dict[str, dict[date, Any]],
    lookback_days: int,
) -> dict[str, Any]:
    yahoo_ohlc: dict[str, dict[str, list[str]]] = {}
    for ticker, bars in retained.items():
        yahoo_ohlc[ticker] = {
            day.isoformat(): [str(bar.open), str(bar.high), str(bar.low), str(bar.close)]
            for day, bar in bars.items()
        }
    return {
        **_dump_window(window),
        "lookback_days": lookback_days,
        "written": written,
        "unresolved": [t.key for t in outcome.unresolved],
        "yahoo_ohlc": yahoo_ohlc,
    }


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
def capture_prices_task(
    self: Any,
    market: str,
    session_node: str,
    retry_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture one (market, session_node) into price_snapshots."""
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.services.price_capture import (
        capture_prices,
        capture_prices_attempt,
        emit_etf_terminal_diagnostics,
    )

    if market != "A-Share" or session_node != "close":
        session = SessionLocal()
        try:
            written = capture_prices(session, market, session_node)
            return {"market": market, "session_node": session_node, "written": written}
        except Retry:
            raise
        except Exception as exc:
            logger.exception("capture_prices_task: failed for %s/%s", market, session_node)
            if self.request.retries >= self.max_retries:
                _capture_failed(
                    "capture_prices_task",
                    exc,
                    context=f"market={market} session_node={session_node}",
                )
            raise self.retry(exc=exc) from exc
        finally:
            session.close()

    settings = get_settings()
    lookback_days = 7
    session = SessionLocal()
    try:
        window, only_tickers, prior_written, anchors = _load_price_retry(
            retry_context, lookback_days, settings.FUND_NAV_MAX_LAG_SESSIONS
        )
        allow_fallback = self.request.retries >= self.max_retries
        outcome, retained = capture_prices_attempt(
            session,
            market,
            session_node,
            window=window,
            lookback_days=lookback_days,
            only_tickers=only_tickers,
            allow_fallback=allow_fallback,
            yahoo_anchors=anchors,
        )
        total_written = prior_written + outcome.written
        if outcome.unresolved and self.request.retries < self.max_retries:
            raise self.retry(
                kwargs={
                    "retry_context": _dump_price_retry(
                        window, outcome, total_written, retained, lookback_days
                    )
                },
                countdown=self.default_retry_delay,
            )
        if outcome.unresolved:
            emit_etf_terminal_diagnostics(outcome.unresolved)
            raise CaptureDataMiss(
                "A-Share ETF close capture exhausted retry/fallback with unresolved dates",
                CaptureOutcome(
                    written=total_written,
                    unresolved=outcome.unresolved,
                    recovered=outcome.recovered,
                    history_coverage=outcome.history_coverage,
                ),
            )
        return {
            "market": market,
            "session_node": session_node,
            "written": total_written,
            "recovered": list(outcome.recovered),
            "unresolved": [],
        }
    except Retry:
        raise
    except CaptureDataMiss:
        raise
    except Exception as exc:
        logger.exception("capture_prices_task: failed for %s/%s", market, session_node)
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "capture_prices_task",
                exc,
                context=f"market={market} session_node={session_node}",
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
def capture_fund_navs_task(
    self: Any, retry_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Fetch settled NAV history from Tiantian Fund for fund_code holdings into price_snapshots."""
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.services.price_capture import capture_fund_navs_attempt

    settings = get_settings()
    lookback_days = 30
    session = SessionLocal()
    try:
        window, only_keys, prior_written, fund_codes = _load_nav_retry(
            retry_context, lookback_days, settings.FUND_NAV_MAX_LAG_SESSIONS
        )
        allow_fallback = self.request.retries >= self.max_retries
        outcome = capture_fund_navs_attempt(
            session,
            window=window,
            lookback_days=lookback_days,
            fund_codes=fund_codes,
            only_keys=only_keys,
            allow_fallback=allow_fallback,
        )
        return _finish_nav_task(
            self,
            outcome,
            window,
            prior_written,
            fund_codes,
            lookback_days,
            settings.FUND_NAV_MAX_LAG_SESSIONS,
            task_name="capture_fund_navs_task",
        )
    except Retry:
        raise
    except CaptureDataMiss:
        raise
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
def backfill_fund_navs_task(
    self: Any,
    fund_codes: list[str] | None = None,
    retry_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch settled NAV history for the given fund_codes.

    Dispatched by confirm_holdings for this user's auto-priced funds that have
    no close in price_snapshots. Daily capture stays on capture_fund_navs_task
    (full fund universe, same 30-day lookback). Idempotent on
    (ticker, market, session_node, trade_date). Funds are not a §4.4 series
    (compute_technical_positions skips no-ticker holdings), so this is a
    valuation/anomaly cold-start, not a 420-day OHLCV seed.
    """
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.services.price_capture import capture_fund_navs_attempt

    if not fund_codes and retry_context is None:
        logger.info("backfill_fund_navs_task: no fund_codes requested")
        return {"written": 0}

    settings = get_settings()
    lookback_days = 30
    session = SessionLocal()
    try:
        window, only_keys, prior_written, codes = _load_nav_retry(
            retry_context,
            lookback_days,
            settings.FUND_NAV_MAX_LAG_SESSIONS,
            default_fund_codes=fund_codes,
        )
        allow_fallback = self.request.retries >= self.max_retries
        outcome = capture_fund_navs_attempt(
            session,
            window=window,
            lookback_days=lookback_days,
            fund_codes=codes,
            only_keys=only_keys,
            allow_fallback=allow_fallback,
        )
        return _finish_nav_task(
            self,
            outcome,
            window,
            prior_written,
            codes,
            lookback_days,
            settings.FUND_NAV_MAX_LAG_SESSIONS,
            task_name="backfill_fund_navs_task",
        )
    except Retry:
        raise
    except CaptureDataMiss:
        raise
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


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.capture_portfolio_value_snapshot_task",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def capture_portfolio_value_snapshot_task(self: Any) -> dict[str, int]:
    """Daily Portfolio Performance snapshot (issue #360 Phase 1). Scheduled
    after the day's price-capture and FX-fetch tasks (app/tasks/__init__.py)
    so a user's day almost always resolves its FX dependency on the first
    try; when it doesn't, that user/day is marked `skipped_deps` and the
    catch-up pass below covers it rather than silently understating today's
    value.

    Issue #373: the capture freezes each day's payload before publishing it,
    and a bounded catch-up pass then replays any frozen-but-unpublished day
    and rebuilds a recently missed one whose book provably hasn't moved. Days
    it refuses to invent are logged and left non-`complete` for ops (detection
    is the separate capture-health probe, #372).
    """
    from datetime import date, timedelta

    from app.core.database import SessionLocal
    from app.services.portfolio_history import capture_portfolio_value_snapshot
    from app.services.snapshot_recovery import CATCHUP_LOOKBACK_DAYS, recover_portfolio_snapshots

    session = SessionLocal()
    try:
        result = capture_portfolio_value_snapshot(session)
        today = date.today()
        recovery = recover_portfolio_snapshots(
            session,
            start_date=today - timedelta(days=CATCHUP_LOOKBACK_DAYS),
            end_date=today,
            today=today,
        )
        session.commit()
        return {
            **result,
            "recovered_replayed": recovery.replayed,
            "recovered_recomputed": recovery.recomputed,
        }
    except Exception as exc:
        session.rollback()
        logger.exception("capture_portfolio_value_snapshot_task: failed")
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "capture_portfolio_value_snapshot_task",
                exc,
                context="Portfolio Performance chart will be missing today's data point.",
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.check_capture_health_task",
    bind=True,
    max_retries=1,
    default_retry_delay=300,
)
def check_capture_health_task(self: Any) -> dict[str, object]:
    """21:30 ET Mon-Fri probe (issue #372 slice B). Lag + skipped_deps only."""
    from app.core.database import SessionLocal
    from app.services.capture_health import evaluate_capture_health, maybe_alert_capture_health

    session = SessionLocal()
    try:
        report = evaluate_capture_health(session)
        maybe_alert_capture_health(report)
        return report.as_dict()
    except Exception as exc:
        logger.exception("check_capture_health_task: failed")
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "check_capture_health_task",
                exc,
                context="Capture-health probe failed; lag may be invisible until the next weekday.",
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.capture_fx_catchup_task",
    bind=True,
    max_retries=1,
    default_retry_delay=300,
)
def capture_fx_catchup_task(self: Any) -> dict[str, object]:
    """00:05 ET catch-up for the prior ET weekday's FX rates (issue #426).

    Scheduled `tue-sat` (each run targets the previous ET weekday: tue->mon,
    ..., sat->fri) so `target_date` has fully closed out and the vendor has
    had hours past the ~17:00 ET FX rollover to publish, before this retries
    `update_fx_rates()` once and falls back to Twelve Data per still-missing
    pair. Detection (the #372 stale alert) is unaffected by this — it only
    alerts on its own if a pair is still missing after both attempts.
    """
    from datetime import timedelta

    from app.core.database import SessionLocal
    from app.core.timezones import ET
    from app.services.capture_health import expected_capture_date
    from app.services.fx_fetcher import fx_catchup

    session = SessionLocal()
    try:
        today_et = datetime.now(tz=ET).date()
        target_date = expected_capture_date(today_et - timedelta(days=1))
        result = fx_catchup(session, target_date)
        session.commit()
        return result.as_dict()
    except Exception as exc:
        session.rollback()
        logger.exception("capture_fx_catchup_task: failed")
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "capture_fx_catchup_task",
                exc,
                context="FX catch-up for the prior trading day failed; that day's rate may stay stale.",
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="app.tasks.capture_tasks.capture_benchmark_index_prices_task",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def capture_benchmark_index_prices_task(self: Any) -> dict[str, int]:
    """Daily close capture for the catalog benchmark indexes
    (issue #360 Phase 1, D9; CSI 300 added in issue #383)."""
    from app.core.database import SessionLocal
    from app.services.benchmark_prices import capture_benchmark_index_prices

    session = SessionLocal()
    try:
        written = capture_benchmark_index_prices(session)
        session.commit()
        return {"written": written}
    except Exception as exc:
        session.rollback()
        logger.exception("capture_benchmark_index_prices_task: failed")
        if self.request.retries >= self.max_retries:
            _capture_failed(
                "capture_benchmark_index_prices_task",
                exc,
                context="Portfolio Performance benchmark lines will be missing today's data point.",
            )
        raise self.retry(exc=exc) from exc
    finally:
        session.close()
