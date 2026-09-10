"""Capture price snapshots into `price_snapshots` (ADR-002 capture layer).

Credit-free (yfinance). The `close` node stores the authoritative daily OHLCV
bar; intraday nodes (pre_open / open / after_close) store a best-effort `last`
(null when yfinance has no intraday value). Idempotent upsert on
(ticker, market, session_node, trade_date) so catch-up re-runs overwrite rather
than duplicate. FX is NOT captured here — it stays in `fx_rates`.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import httpx
from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.alert_dedup import already_alerted, mark_alerted
from app.core.config import get_settings
from app.core.timezones import CST, MARKET_TZ
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services._finnhub import fetch_quotes as fetch_finnhub_quotes
from app.services._massive import fetch_prev_close_ohlcv as fetch_massive_prev_close_ohlcv
from app.services._tencent import (
    OhlcBar,
    admit_tencent_missing_dates,
    fetch_tencent_daily_bars,
)
from app.services._yfinance import fetch_ohlcv_range, fetch_ohlcv_range_bounded, fetch_spot
from app.services.capture_results import (
    CaptureOutcome,
    CaptureTarget,
    NavPoint,
    is_positive_finite,
)
from app.services.china_session_calendar import (
    ChinaSessionWindow,
    completed_sessions,
    freeze_capture_window,
)
from app.services.email_sender import send_ops_alert
from app.services.fund_nav_fetcher import fetch_nav_history_outcome, sina_latest_nav_point
from app.services.instrument_symbols import (
    InstrumentKey,
    build_provider_request_plan,
    normalize_legacy_ticker,
    to_provider_symbol,
)
from app.services.markets import is_capture_supported, market_from_ticker

logger = logging.getLogger(__name__)

# PostgreSQL/psycopg hard-cap a single query at 65535 bound parameters.
# Close-node rows bind 10 params each, so 6553 rows is the theoretical
# ceiling; 2000 leaves margin if a future caller adds columns. Issue #194.
_UPSERT_CHUNK_SIZE = 2000


def _effective_market(h: Holding) -> str:
    """User-declared market wins; otherwise derive from the ticker.

    Unknown suffixes resolve to Other. Capture still keys off
    ``is_capture_supported`` and never fetches Other.
    """
    if h.market:
        return h.market
    inferred = market_from_ticker(h.ticker)
    if inferred:
        return inferred
    if h.fund_code:
        return "A-Share"
    return "Other"


def _market_tickers(session: Session, market: str) -> list[str]:
    """Auto-priced holdings tickers whose effective market is `market`.

    Holdings with capture_supported=False are never included, so capture
    never attempts a speculative yfinance lookup for unsupported markets.
    """
    holdings = session.execute(
        select(Holding).where(Holding.ticker.is_not(None), Holding.pricing_mode == "auto")
    ).scalars()
    return sorted(
        {
            h.ticker
            for h in holdings
            if h.ticker and is_capture_supported(h) and _effective_market(h) == market
        }
    )


def _upsert(session: Session, rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), _UPSERT_CHUNK_SIZE):
        written += _upsert_chunk(session, rows[start : start + _UPSERT_CHUNK_SIZE])
    return written


def _upsert_chunk(session: Session, rows: list[dict[str, object]]) -> int:
    base = pg_insert(PriceSnapshot).values(rows)
    update_cols = {
        c: base.excluded[c]
        for c in ("open", "high", "low", "close", "last", "volume", "source", "captured_at")
    }
    stmt = base.on_conflict_do_update(
        constraint="uq_price_snapshots_key", set_=update_cols
    ).returning(PriceSnapshot.id)
    n = len(session.execute(stmt).fetchall())
    session.commit()
    return n


_PRICE_AGREE_ABS = literal(Decimal("0.000001"))
_PRICE_AGREE_REL = literal(Decimal("0.000001"))


def _sql_close_unusable() -> ColumnElement[bool]:
    return or_(
        PriceSnapshot.close.is_(None),
        PriceSnapshot.close <= 0,
        PriceSnapshot.close != PriceSnapshot.close,
    )


def _sql_component_agrees(existing: object, proposed: object) -> ColumnElement[bool]:
    existing_col = cast(ColumnElement[Decimal | None], existing)
    proposed_col = cast(ColumnElement[Decimal | None], proposed)
    return or_(
        existing_col.is_(None),
        and_(
            proposed_col.is_not(None),
            func.abs(existing_col - proposed_col)
            <= func.greatest(_PRICE_AGREE_ABS, func.abs(proposed_col) * _PRICE_AGREE_REL),
        ),
    )


def _guarded_fallback_upsert(session: Session, rows: list[dict[str, object]]) -> int:
    """Insert missing close points; never overwrite a usable existing close."""
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), _UPSERT_CHUNK_SIZE):
        written += _guarded_fallback_upsert_chunk(session, rows[start : start + _UPSERT_CHUNK_SIZE])
    return written


def _guarded_fallback_upsert_chunk(session: Session, rows: list[dict[str, object]]) -> int:
    base = pg_insert(PriceSnapshot).values(rows)
    excl = base.excluded
    update_cols = {
        "open": excl.open,
        "high": excl.high,
        "low": excl.low,
        "close": excl.close,
        "source": excl.source,
        "captured_at": excl.captured_at,
        "last": func.coalesce(PriceSnapshot.last, excl.last),
        "volume": func.coalesce(PriceSnapshot.volume, excl.volume),
    }
    stmt = base.on_conflict_do_update(
        constraint="uq_price_snapshots_key",
        set_=update_cols,
        where=and_(
            _sql_close_unusable(),
            _sql_component_agrees(PriceSnapshot.open, excl.open),
            _sql_component_agrees(PriceSnapshot.high, excl.high),
            _sql_component_agrees(PriceSnapshot.low, excl.low),
        ),
    ).returning(PriceSnapshot.id)
    n = len(session.execute(stmt).fetchall())
    session.commit()
    return n


def capture_prices(
    session: Session,
    market: str,
    session_node: str,
    trade_date: date | None = None,
    lookback_days: int = 7,
    tickers: list[str] | None = None,
) -> int:
    """Capture one (market, session_node) into price_snapshots. Returns rows written.

    close node → daily OHLCV over the last `lookback_days` (each bar keyed by its
    own trade_date, so missed days are backfilled); other nodes → best-effort
    `last` (trade_date = today in the market's local clock).

    `tickers` restricts the fetch to that subset (confirm-time OHLCV backfill).
    ``None`` keeps the daily path's full auto-priced market universe.
    """
    selected = _market_tickers(session, market)
    if tickers is not None:
        wanted = set(tickers)
        selected = [t for t in selected if t in wanted]
    if not selected:
        logger.info("capture_prices: no auto tickers for market %s", market)
        return 0

    now = datetime.now(tz=UTC)
    rows: list[dict[str, object]] = []

    if session_node == "close":
        ohlcv = fetch_ohlcv_range(selected, lookback_days=lookback_days)
        for ticker, bars in ohlcv.items():
            for bar_date, o, h, low, c, vol in bars:
                rows.append(
                    {
                        "ticker": ticker,
                        "market": market,
                        "session_node": session_node,
                        "trade_date": bar_date,
                        "open": o,
                        "high": h,
                        "low": low,
                        "close": c,
                        "volume": vol,
                        "captured_at": now,
                    }
                )

        # Massive.com fallback (issue #56): free-tier EOD-only, current
        # trading day withheld — matches this node's semantics exactly (it
        # wants the finalized prior-session bar, never same-day intraday).
        # US-only, close-node-only: never wired into pre_open/open/
        # after_close (structurally cannot serve same-day data) or any
        # non-US market (free tier is US-stocks-only, officially, at every
        # paid tier too).
        # ohlcv is keyed by fetch_ohlcv_range's normalized ticker (e.g. "PSH"
        # -> "PSH.L" via the shared override table) — comparing the raw
        # selected ticker against those keys always misses for any ticker
        # that normalizes, wrongly flagging a clean yfinance hit as missing
        # (issue #351). `missing_keys` itself is built from the normalized
        # form too: a genuine miss must still ask the fallback for the right
        # instrument, not risk the same raw-ticker collision #204 fixed for
        # the primary yfinance lookup. Issue #57 stage 57-2: the actual
        # provider request/response mapping now goes through a forward-built
        # plan (`build_provider_request_plan`) instead of trusting the
        # provider's echoed key verbatim — an unmatched response key is
        # omitted with a diagnostic rather than written straight to
        # PriceSnapshot.
        missing_keys = [
            InstrumentKey("ticker", normalize_legacy_ticker(t))
            for t in selected
            if normalize_legacy_ticker(t) not in ohlcv
        ]
        massive_key = get_settings().MASSIVE_API_KEY
        if market == "US" and massive_key is not None and missing_keys:
            massive_plan = build_provider_request_plan("massive", missing_keys)
            if massive_plan.unsupported:
                # A missing code with no US-eligible wire symbol (e.g. a
                # holding declared market=US whose legacy lookup key is a
                # non-US suffix, like the historical PSH -> PSH.L alias)
                # must not be sent to a US-only fallback — issue #57 stage
                # 57-2 correction, matches the frozen provider table.
                logger.info(
                    "capture_prices: massive fallback skipping unsupported code(s): %s",
                    massive_plan.unsupported,
                )
            if massive_plan.wire_symbols:
                for wire_ticker, bar in fetch_massive_prev_close_ohlcv(
                    list(massive_plan.wire_symbols), massive_key.get_secret_value()
                ).items():
                    internal_ticker = massive_plan.to_internal.get(wire_ticker)
                    if internal_ticker is None:
                        logger.warning(
                            "capture_prices: massive returned unmatched wire symbol %s; omitting",
                            wire_ticker,
                        )
                        continue
                    bar_date, o, h, low, c, vol = bar
                    logger.info(
                        "capture_prices: massive fallback used for %s %s",
                        internal_ticker,
                        bar_date,
                    )
                    rows.append(
                        {
                            "ticker": internal_ticker,
                            "market": market,
                            "session_node": session_node,
                            "trade_date": bar_date,
                            "open": o,
                            "high": h,
                            "low": low,
                            "close": c,
                            "volume": vol,
                            "source": "massive",
                            "captured_at": now,
                        }
                    )
    else:
        td = trade_date or datetime.now(tz=MARKET_TZ.get(market, UTC)).date()
        spot = fetch_spot(selected)
        for ticker, last in spot.items():
            rows.append(
                {
                    "ticker": ticker,
                    "market": market,
                    "session_node": session_node,
                    "trade_date": td,
                    "last": last,
                    "captured_at": now,
                }
            )

        # Finnhub fallback (issue #56): near-real-time single point, a fit
        # for spot/intraday nodes — never the close node, which uses
        # fetch_ohlcv_range and wants finalized daily bars, not a quote.
        # US-only by verified free-tier capability (non-US symbols return
        # {"error": ...}), so this only runs for the US market bucket.
        # Same normalized-key mismatch as the close branch above, including
        # `missing_keys` itself being built from the normalized form so a
        # real miss doesn't hand the fallback a raw, possibly wrong-
        # instrument ticker (issue #351). Same forward-built request plan as
        # the massive branch above (issue #57 stage 57-2).
        missing_keys = [
            InstrumentKey("ticker", normalize_legacy_ticker(t))
            for t in selected
            if normalize_legacy_ticker(t) not in spot
        ]
        finnhub_key = get_settings().FINNHUB_API_KEY
        if market == "US" and finnhub_key is not None and missing_keys:
            finnhub_plan = build_provider_request_plan("finnhub", missing_keys)
            if finnhub_plan.unsupported:
                # Same US-eligibility correction as the massive branch above
                # (issue #57 stage 57-2) — a missing code with no US wire
                # symbol must not be sent to a US-only fallback.
                logger.info(
                    "capture_prices: finnhub fallback skipping unsupported code(s): %s",
                    finnhub_plan.unsupported,
                )
            if finnhub_plan.wire_symbols:
                for wire_ticker, quote in fetch_finnhub_quotes(
                    list(finnhub_plan.wire_symbols), finnhub_key.get_secret_value()
                ).items():
                    internal_ticker = finnhub_plan.to_internal.get(wire_ticker)
                    if internal_ticker is None:
                        logger.warning(
                            "capture_prices: finnhub returned unmatched wire symbol %s; omitting",
                            wire_ticker,
                        )
                        continue
                    logger.info("capture_prices: finnhub fallback used for %s", internal_ticker)
                    rows.append(
                        {
                            "ticker": internal_ticker,
                            "market": market,
                            "session_node": session_node,
                            "trade_date": td,
                            "last": quote.last,
                            "source": "finnhub",
                            "captured_at": now,
                        }
                    )

    written = _upsert(session, rows)
    logger.info(
        "capture_prices: market=%s node=%s tickers=%d written=%d",
        market,
        session_node,
        len(selected),
        written,
    )
    return written


def _auto_fund_codes(session: Session) -> dict[str, str]:
    """Unique fund_code -> market for auto-priced fund holdings.

    Two holdings of the same fund (two users, or two lots) must not produce
    duplicate upsert rows — Postgres rejects ON CONFLICT DO UPDATE when one
    INSERT proposes the same key twice.
    """
    holdings = session.execute(
        select(Holding).where(
            Holding.pricing_mode == "auto",
            Holding.fund_code.is_not(None),
        )
    ).scalars()
    declared: dict[str, set[str]] = {}
    for h in holdings:
        code = h.fund_code
        if not code:
            continue
        declared.setdefault(code, set())
        if h.market:
            declared[code].add(h.market)
    # Prefer an explicitly declared market over the A-Share default; if
    # several lots disagree, the lexicographically smallest wins so the
    # upsert key does not depend on query order.
    return {code: (min(markets) if markets else "A-Share") for code, markets in declared.items()}


def _cst_today() -> date:
    """Today's date in China Standard Time — the fund NAV task's own clock."""
    return datetime.now(tz=CST).date()


# Dedup keys embed the NAV date (or CST date for the empty case), so this TTL
# is a garbage-collection safety net only — a changed state makes a new key.
_ALERT_DEDUP_TTL_SECONDS = 90 * 24 * 60 * 60


def _send_nav_alert(subject: str, body: str, dedup_key: str) -> None:
    """Send a fund-NAV ops alert unless this dedup_key was already alerted.

    The durable Redis dedup is the real anti-daily-spam mechanism; the Resend
    Idempotency-Key (`idempotency_key=dedup_key`) only collapses same-task
    retries within its 24h window, which is not enough for a 24h-apart
    weekday beat (issue #298 review). The dedup key is recorded only when
    delivery actually succeeded — send_ops_alert never raises, so its bool
    return is the only delivery signal; a failed send must leave the state
    un-deduped so the next beat retries it (round-2 review).
    """
    if already_alerted(dedup_key):
        return
    if send_ops_alert(subject=subject, body=body, idempotency_key=dedup_key):
        mark_alerted(dedup_key, _ALERT_DEDUP_TTL_SECONDS)


def _warn_if_nav_missing(fund_code: str, as_of_date: date) -> None:
    """WARNING + durable-deduped ops alert when a fund returns no NAV history."""
    logger.warning(
        "capture_fund_navs: fund %s returned no NAV history (fetch miss) on %s",
        fund_code,
        as_of_date.isoformat(),
    )
    _send_nav_alert(
        subject=f"[Portfonia] fund NAV missing — {fund_code}",
        body=(
            f"capture_fund_navs_task got no usable NAV for fund {fund_code} on "
            f"{as_of_date.isoformat()} (CST) after bounded Eastmoney retry and Sina "
            f"latest-NAV fallback. Other funds in the same run may still have written "
            f"rows, so the aggregate write count alone cannot surface this miss.\n\n"
            f"Check worker.log for fetch errors mentioning this code."
        ),
        dedup_key=f"ops-fund-nav-empty-{fund_code}-{as_of_date.isoformat()}",
    )


def _warn_if_nav_stale(
    fund_code: str,
    latest_nav_date: date,
    as_of_date: date,
    max_lag_sessions: int,
) -> None:
    """WARNING + durable-deduped ops alert when the latest NAV exceeds the common lag."""
    logger.warning(
        "capture_fund_navs: fund %s latest NAV %s exceeds operational lag of %d "
        "completed China session(s) as_of %s",
        fund_code,
        latest_nav_date.isoformat(),
        max_lag_sessions,
        as_of_date.isoformat(),
    )
    _send_nav_alert(
        subject=f"[Portfonia] fund NAV stale — {fund_code}",
        body=(
            f"capture_fund_navs_task found fund {fund_code} with latest NAV dated "
            f"{latest_nav_date.isoformat()}, older than the common operational "
            f"tolerance of {max_lag_sessions} completed China exchange session(s) "
            f"as of {as_of_date.isoformat()} (CST).\n\n"
            f"This is a capture/alert threshold, not a claim that the fund missed a "
            f"disclosure deadline. Check price_snapshots and worker.log for "
            f"capture_fund_navs_task runs mentioning this code."
        ),
        dedup_key=f"ops-fund-nav-stale-{fund_code}-{latest_nav_date.isoformat()}",
    )


def emit_nav_terminal_diagnostics(
    targets: tuple[CaptureTarget, ...],
    as_of_date: date,
    max_lag_sessions: int,
) -> None:
    """Send per-fund empty/stale alerts after retry/fallback exhaustion."""
    for target in targets:
        if target.kind != "nav":
            continue
        if target.reason == "lag_exceeded":
            _warn_if_nav_stale(
                target.key,
                target.latest_date or as_of_date,
                as_of_date,
                max_lag_sessions,
            )
        else:
            _warn_if_nav_missing(target.key, as_of_date)


def emit_etf_terminal_diagnostics(targets: tuple[CaptureTarget, ...]) -> None:
    for target in targets:
        if target.kind != "etf_close":
            continue
        for missing in target.missing_dates:
            dedup_key = f"ops-etf-close-missing-{target.key}-{missing.isoformat()}"
            logger.warning(
                "capture_prices: ETF %s missing close %s after Yahoo retry and Tencent fallback",
                target.key,
                missing.isoformat(),
            )
            _send_nav_alert(
                subject=f"[Portfonia] ETF close missing — {target.key} {missing.isoformat()}",
                body=(
                    f"A-Share ETF {target.key} is missing a China-session close on "
                    f"{missing.isoformat()} after bounded Yahoo retry and Tencent daily "
                    f"fallback (reason={target.reason}). Existing rows were preserved."
                ),
                dedup_key=dedup_key,
            )


def _fund_currency_sets(session: Session) -> dict[str, set[str]]:
    holdings = session.execute(
        select(Holding).where(
            Holding.pricing_mode == "auto",
            Holding.fund_code.is_not(None),
        )
    ).scalars()
    out: dict[str, set[str]] = {}
    for holding in holdings:
        code = holding.fund_code
        if not code:
            continue
        out.setdefault(code, set()).add(holding.currency)
    return out


def _valid_close_date(row: PriceSnapshot) -> date | None:
    if row.close is None:
        return None
    close = Decimal(row.close)
    if not is_positive_finite(close):
        return None
    return row.trade_date


def _latest_stored_nav_date(session: Session, fund_code: str, market: str) -> date | None:
    rows = session.execute(
        select(PriceSnapshot).where(
            PriceSnapshot.ticker == fund_code,
            PriceSnapshot.market == market,
            PriceSnapshot.session_node == "close",
        )
    ).scalars()
    dates = [d for d in (_valid_close_date(row) for row in rows) if d is not None]
    return max(dates) if dates else None


def _nav_rows(
    fund_code: str, market: str, points: tuple[NavPoint, ...], captured_at: datetime
) -> list[dict[str, object]]:
    return [
        {
            "ticker": fund_code,
            "market": market,
            "session_node": "close",
            "trade_date": point.nav_date,
            "close": point.unit_nav,
            "source": point.source,
            "captured_at": captured_at,
        }
        for point in points
    ]


def _classify_nav_target(
    fund_code: str,
    market: str,
    window: ChinaSessionWindow,
    latest: date | None,
    fetch_error: str | None,
    source_failed: bool,
) -> CaptureTarget | None:
    reason: str | None
    if source_failed:
        reason = fetch_error or "missing"
    elif latest is None:
        reason = "missing" if window.calendar_status == "ok" else "calendar_unknown"
    elif window.calendar_status != "ok" or (window.cutoff is not None and latest >= window.cutoff):
        return None
    else:
        reason = "lag_exceeded"
    return CaptureTarget(
        key=fund_code,
        market=market,
        kind="nav",
        reason=reason,
        cutoff=window.cutoff,
        latest_date=latest,
        window_start=window.window_start,
        window_end=window.window_end,
    )


def capture_fund_navs_attempt(
    session: Session,
    *,
    window: ChinaSessionWindow,
    lookback_days: int = 30,
    fund_codes: list[str] | None = None,
    only_keys: tuple[str, ...] | None = None,
    allow_fallback: bool = False,
) -> CaptureOutcome:
    """One primary (and optional Sina) attempt. Commits accepted rows first."""
    selected = _auto_fund_codes(session)
    currencies = _fund_currency_sets(session)
    if fund_codes is not None:
        wanted = set(fund_codes)
        selected = {code: market for code, market in selected.items() if code in wanted}
    if only_keys is not None:
        keep = set(only_keys)
        selected = {code: market for code, market in selected.items() if code in keep}
    if not selected:
        logger.info("capture_fund_navs: no fund_code holdings to capture")
        return CaptureOutcome(written=0, unresolved=(), recovered=(), history_coverage={})

    now = datetime.now(tz=UTC)
    as_of_date = window.window_end
    written = 0
    coverage: dict[str, str] = {}
    unresolved: list[CaptureTarget] = []
    recovered: list[str] = []

    with httpx.Client() as client:
        for fund_code, market in selected.items():
            outcome = fetch_nav_history_outcome(
                fund_code,
                client,
                start_date=window.window_start,
                end_date=window.window_end,
                as_of_date=as_of_date,
            )
            if outcome.points:
                written += _upsert(session, _nav_rows(fund_code, market, outcome.points, now))
                coverage[fund_code] = "primary_window"
            latest = None
            if outcome.points:
                latest = max(point.nav_date for point in outcome.points)
            stored = _latest_stored_nav_date(session, fund_code, market)
            if stored is not None and (latest is None or stored > latest):
                latest = stored
            source_failed = outcome.error is not None or not outcome.points
            target = _classify_nav_target(
                fund_code, market, window, latest, outcome.error, source_failed
            )
            if target is None:
                continue
            if allow_fallback:
                currency_ok = currencies.get(fund_code) == {"CNY"}
                sina_point: NavPoint | None = None
                if currency_ok:
                    sina_point = sina_latest_nav_point(fund_code, client, as_of_date)
                elif currencies.get(fund_code) not in (None, {"CNY"}):
                    target = CaptureTarget(
                        key=fund_code,
                        market=market,
                        kind="nav",
                        reason="unsupported_currency",
                        cutoff=window.cutoff,
                        latest_date=latest,
                        window_start=window.window_start,
                        window_end=window.window_end,
                    )
                if sina_point is not None:
                    n = _guarded_fallback_upsert(
                        session, _nav_rows(fund_code, market, (sina_point,), now)
                    )
                    written += n
                    stored = _latest_stored_nav_date(session, fund_code, market)
                    latest = stored
                    if coverage.get(fund_code) != "primary_window":
                        coverage[fund_code] = "latest_only"
                    recovered.append(fund_code)
                still = _classify_nav_target(
                    fund_code, market, window, latest, None, source_failed=False
                )
                if still is None:
                    if sina_point is None:
                        logger.info(
                            "capture_fund_navs: fund %s primary source failed; stored latest %s still usable",
                            fund_code,
                            latest.isoformat() if latest else "none",
                        )
                    continue
                unresolved.append(still)
                continue
            unresolved.append(target)

    logger.info(
        "capture_fund_navs: funds=%d written=%d unresolved=%d",
        len(selected),
        written,
        len(unresolved),
    )
    return CaptureOutcome(
        written=written,
        unresolved=tuple(unresolved),
        recovered=tuple(recovered),
        history_coverage=coverage,
    )


def capture_fund_navs(
    session: Session,
    lookback_days: int = 30,
    fund_codes: list[str] | None = None,
) -> int:
    """Compatibility wrapper: one primary attempt, no fallback, no terminal email."""
    settings = get_settings()
    window = freeze_capture_window(
        datetime.now(tz=UTC), lookback_days, settings.FUND_NAV_MAX_LAG_SESSIONS
    )
    outcome = capture_fund_navs_attempt(
        session,
        window=window,
        lookback_days=lookback_days,
        fund_codes=fund_codes,
        allow_fallback=False,
    )
    return outcome.written


def _yahoo_bar(point: tuple[date, float, float, float, float, float | None]) -> OhlcBar | None:
    trade_date, open_, high, low, close, _volume = point
    try:
        bar = OhlcBar(
            trade_date=trade_date,
            open=Decimal(str(open_)),
            high=Decimal(str(high)),
            low=Decimal(str(low)),
            close=Decimal(str(close)),
        )
    except Exception:
        return None
    if not (
        is_positive_finite(bar.open)
        and is_positive_finite(bar.high)
        and is_positive_finite(bar.low)
        and is_positive_finite(bar.close)
    ):
        return None
    if not (bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high):
        return None
    return bar


def _eligible_china_etf_tickers(session: Session, selected: list[str]) -> dict[str, str]:
    """canonical ticker -> market for Tencent-eligible A-Share ETFs."""
    holdings = session.execute(
        select(Holding).where(Holding.ticker.is_not(None), Holding.pricing_mode == "auto")
    ).scalars()
    wanted = set(selected)
    grouped: dict[str, list[Holding]] = {}
    for holding in holdings:
        if not holding.ticker or holding.ticker not in wanted:
            continue
        if not is_capture_supported(holding) or _effective_market(holding) != "A-Share":
            continue
        grouped.setdefault(normalize_legacy_ticker(holding.ticker), []).append(holding)
    eligible: dict[str, str] = {}
    for ticker, rows in grouped.items():
        wire = to_provider_symbol("tencent", InstrumentKey("ticker", ticker))
        if wire is None:
            continue
        types = {row.asset_type for row in rows}
        currencies = {row.currency for row in rows}
        if types != {"etf"} or currencies != {"CNY"}:
            logger.info(
                "capture_prices: skipping Tencent for %s (type=%s currency=%s)",
                ticker,
                types,
                currencies,
            )
            continue
        eligible[ticker] = "A-Share"
    return eligible


def _stored_valid_close_dates(session: Session, ticker: str, market: str) -> set[date]:
    rows = session.execute(
        select(PriceSnapshot).where(
            PriceSnapshot.ticker == ticker,
            PriceSnapshot.market == market,
            PriceSnapshot.session_node == "close",
        )
    ).scalars()
    return {d for d in (_valid_close_date(row) for row in rows) if d is not None}


def capture_prices_attempt(
    session: Session,
    market: str,
    session_node: str,
    *,
    window: ChinaSessionWindow,
    lookback_days: int = 7,
    tickers: list[str] | None = None,
    only_tickers: tuple[str, ...] | None = None,
    allow_fallback: bool = False,
    yahoo_anchors: dict[str, dict[date, OhlcBar]] | None = None,
) -> tuple[CaptureOutcome, dict[str, dict[date, OhlcBar]]]:
    """A-Share daily close attempt with optional Tencent gap fill."""
    if market != "A-Share" or session_node != "close":
        written = capture_prices(
            session, market, session_node, lookback_days=lookback_days, tickers=tickers
        )
        return (
            CaptureOutcome(written=written, unresolved=(), recovered=(), history_coverage={}),
            {},
        )

    selected = _market_tickers(session, market)
    if tickers is not None:
        wanted = set(tickers)
        selected = [t for t in selected if t in wanted]
    if only_tickers is not None:
        keep = set(only_tickers)
        selected = [t for t in selected if t in keep]
    if not selected:
        logger.info("capture_prices: no auto tickers for market %s", market)
        return CaptureOutcome(0, (), (), {}), {}

    now = datetime.now(tz=UTC)
    ohlcv = fetch_ohlcv_range_bounded(
        selected, window.window_start, window.window_end + timedelta(days=1)
    )
    rows: list[dict[str, object]] = []
    retained: dict[str, dict[date, OhlcBar]] = {
        ticker: dict(bars) for ticker, bars in (yahoo_anchors or {}).items()
    }
    for ticker, bars in ohlcv.items():
        for point in bars:
            bar = _yahoo_bar(point)
            if bar is None:
                continue
            retained.setdefault(ticker, {})[bar.trade_date] = bar
            rows.append(
                {
                    "ticker": ticker,
                    "market": market,
                    "session_node": session_node,
                    "trade_date": bar.trade_date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": point[5],
                    "source": "yfinance",
                    "captured_at": now,
                }
            )
    written = _upsert(session, rows)

    sessions = completed_sessions(window.as_of_utc, window.window_start, window.window_end)
    eligible = _eligible_china_etf_tickers(session, selected)
    unresolved: list[CaptureTarget] = []
    recovered: list[str] = []
    for ticker, etf_market in eligible.items():
        stored = _stored_valid_close_dates(session, ticker, etf_market)
        yahoo_dates = set(retained.get(ticker, {}))
        observed = stored | yahoo_dates
        if not observed:
            unresolved.append(
                CaptureTarget(
                    key=ticker,
                    market=etf_market,
                    kind="etf_close",
                    reason="missing" if sessions is not None else "calendar_unknown",
                    window_start=window.window_start,
                    window_end=window.window_end,
                )
            )
            continue
        if sessions is None:
            continue
        earliest = min(observed)
        missing = tuple(day for day in sessions if day >= earliest and day not in stored)
        if not missing:
            continue
        unresolved.append(
            CaptureTarget(
                key=ticker,
                market=etf_market,
                kind="etf_close",
                reason="missing",
                missing_dates=missing,
                window_start=window.window_start,
                window_end=window.window_end,
            )
        )

    if allow_fallback:
        still: list[CaptureTarget] = []
        for target in unresolved:
            if target.reason != "missing" or not target.missing_dates:
                still.append(target)
                continue
            wire = to_provider_symbol("tencent", InstrumentKey("ticker", target.key))
            if wire is None:
                still.append(target)
                continue
            raw = fetch_tencent_daily_bars(wire, window.window_start, window.window_end, "raw")
            qfq = fetch_tencent_daily_bars(wire, window.window_start, window.window_end, "qfq")
            expected = sessions or ()
            admitted, reason = admit_tencent_missing_dates(
                target.missing_dates,
                retained.get(target.key, {}),
                raw,
                qfq,
                expected,
            )
            if reason is not None or not admitted:
                still.append(
                    CaptureTarget(
                        key=target.key,
                        market=target.market,
                        kind="etf_close",
                        reason=reason or "unsupported_adjustment",
                        missing_dates=target.missing_dates,
                        window_start=target.window_start,
                        window_end=target.window_end,
                    )
                )
                continue
            fallback_rows: list[dict[str, object]] = [
                {
                    "ticker": target.key,
                    "market": target.market,
                    "session_node": "close",
                    "trade_date": bar.trade_date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": None,
                    "last": None,
                    "source": "tencent",
                    "captured_at": now,
                }
                for bar in admitted.values()
            ]
            written += _guarded_fallback_upsert(session, fallback_rows)
            recovered.append(target.key)
            stored = _stored_valid_close_dates(session, target.key, target.market)
            leftover = tuple(day for day in target.missing_dates if day not in stored)
            if leftover:
                still.append(
                    CaptureTarget(
                        key=target.key,
                        market=target.market,
                        kind="etf_close",
                        reason="missing",
                        missing_dates=leftover,
                        window_start=target.window_start,
                        window_end=target.window_end,
                    )
                )
        unresolved = still

    logger.info(
        "capture_prices: market=%s node=%s tickers=%d written=%d unresolved=%d",
        market,
        session_node,
        len(selected),
        written,
        len(unresolved),
    )
    return (
        CaptureOutcome(
            written=written,
            unresolved=tuple(unresolved),
            recovered=tuple(recovered),
            history_coverage={},
        ),
        retained,
    )
