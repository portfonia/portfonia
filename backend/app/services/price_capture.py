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

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.alert_dedup import already_alerted, mark_alerted
from app.core.timezones import CST, MARKET_TZ
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services._yfinance import _market_key_for_ticker, fetch_ohlcv_range, fetch_spot
from app.services.email_sender import send_ops_alert

logger = logging.getLogger(__name__)

_MARKET_KEY = {"us": "US", "hk": "HK", "cn": "A-Share"}

# PostgreSQL/psycopg hard-cap a single query at 65535 bound parameters.
# Close-node rows bind 10 params each, so 6553 rows is the theoretical
# ceiling; 2000 leaves margin if a future caller adds columns. Issue #194.
_UPSERT_CHUNK_SIZE = 2000


def _effective_market(h: Holding) -> str:
    """User-declared market wins; otherwise derive from the ticker."""
    if h.market:
        return h.market
    return _MARKET_KEY.get(_market_key_for_ticker(h.ticker or ""), "Other")


def _market_tickers(session: Session, market: str) -> list[str]:
    """Auto-priced holdings tickers whose effective market is `market`."""
    holdings = session.execute(
        select(Holding).where(Holding.ticker.is_not(None), Holding.pricing_mode == "auto")
    ).scalars()
    return sorted({h.ticker for h in holdings if h.ticker and _effective_market(h) == market})


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
        for ticker, bars in fetch_ohlcv_range(selected, lookback_days=lookback_days).items():
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
    else:
        td = trade_date or datetime.now(tz=MARKET_TZ.get(market, UTC)).date()
        for ticker, last in fetch_spot(selected).items():
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


def _session_lag(nav_date: date, today: date) -> int:
    """A-share trading sessions in (nav_date, today], approximated as weekdays.

    No China holiday table exists in the codebase, so weekdays stand in for
    the XSHG session calendar: weekends are handled correctly, long holiday
    weeks are not (they can over-count — swap in a real market calendar if
    logs ever show false positives).
    """
    lag = 0
    cursor = nav_date + timedelta(days=1)
    while cursor <= today:
        if cursor.weekday() < 5:
            lag += 1
        cursor += timedelta(days=1)
    return lag


# Issue #298: a healthy fund publishes same-evening NAV, so the freshest NAV
# should be at most one A-share trading session behind today (CST). Friday
# NAV on a Monday before the Monday-evening publish is that expected 1-session
# lag, not a 3-calendar-day gap.
_FUND_NAV_MAX_SESSION_LAG = 1
# Dedup keys embed the NAV date (or CST date for the empty case), so this TTL
# is a garbage-collection safety net only — a changed state makes a new key.
_ALERT_DEDUP_TTL_SECONDS = 90 * 24 * 60 * 60


def _send_nav_alert(subject: str, body: str, dedup_key: str) -> None:
    """Send a fund-NAV ops alert unless this dedup_key was already alerted.

    The durable Redis dedup is the real anti-daily-spam mechanism; the Resend
    Idempotency-Key (`idempotency_key=dedup_key`) only collapses same-task
    retries within its 24h window, which is not enough for a 24h-apart
    weekday beat (issue #298 review).
    """
    if already_alerted(dedup_key):
        return
    send_ops_alert(subject=subject, body=body, idempotency_key=dedup_key)
    mark_alerted(dedup_key, _ALERT_DEDUP_TTL_SECONDS)


def _warn_if_nav_missing(fund_code: str) -> None:
    """WARNING + durable-deduped ops alert when a fund returns no NAV history.

    fetch_nav_history swallows HTTP/parse errors as [] (issue #196), and a
    per-fund miss is invisible in the aggregate `written=N` INFO log when a
    mixed run writes rows for other funds and [] for one code. Keyed per
    (fund_code, CST date) so a persistent miss re-surfaces daily.
    """
    today = _cst_today()
    logger.warning(
        "capture_fund_navs: fund %s returned no NAV history (fetch miss) on %s",
        fund_code,
        today.isoformat(),
    )
    _send_nav_alert(
        subject=f"[Portfonia] fund NAV missing — {fund_code}",
        body=(
            f"capture_fund_navs_task got no NAV history for fund {fund_code} on "
            f"{today.isoformat()} (CST): fetch_nav_history returned [] (HTTP/parse "
            f"error, or no rows in the lookback window).\n\n"
            f"Other funds in the same run may still have written rows, so the aggregate "
            f"INFO log alone cannot surface this miss. Check worker.log for fetch "
            f"errors mentioning this code."
        ),
        dedup_key=f"ops-fund-nav-empty-{fund_code}-{today.isoformat()}",
    )


def _warn_if_nav_stale(fund_code: str, nav_history: list[tuple[date, Decimal]]) -> None:
    """WARNING + durable-deduped ops alert when the latest NAV is stale.

    Observability only (issue #298): does not alter capture behavior. Expects
    `nav_history` sorted ascending (fetch_nav_history's contract), so the last
    entry is the freshest. Stale means more than `_FUND_NAV_MAX_SESSION_LAG`
    trading sessions behind today — Thursday NAV on Monday (513500's shape)
    alerts, Friday NAV on Monday does not. Keyed per (fund_code, NAV date) so
    a stuck date emails once until the date changes.
    """
    latest_nav_date = nav_history[-1][0]
    lag = _session_lag(latest_nav_date, _cst_today())
    if lag <= _FUND_NAV_MAX_SESSION_LAG:
        return
    logger.warning(
        "capture_fund_navs: fund %s latest NAV %s is %d trading session(s) behind today (CST)",
        fund_code,
        latest_nav_date.isoformat(),
        lag,
    )
    _send_nav_alert(
        subject=f"[Portfonia] fund NAV stale — {fund_code}",
        body=(
            f"capture_fund_navs_task found fund {fund_code} with latest NAV dated "
            f"{latest_nav_date.isoformat()}, {lag} trading session(s) behind today (CST).\n\n"
            f"Healthy funds in the same run capture a same-day or 1-session-late NAV; "
            f"this fund has not progressed. The gap may be a late evening NAV publish "
            f"or a capture-task under-delivery (issue #135).\n\n"
            f"Check price_snapshots and worker.log for capture_fund_navs_task runs "
            f"mentioning this code."
        ),
        dedup_key=f"ops-fund-nav-stale-{fund_code}-{latest_nav_date.isoformat()}",
    )


def capture_fund_navs(
    session: Session,
    lookback_days: int = 30,
    fund_codes: list[str] | None = None,
) -> int:
    """Fetch settled NAV history from Tiantian Fund for fund_code holdings.

    Upserts into price_snapshots using fund_code as ticker key, market from the
    holding (defaulting to A-Share), session_node='close'. The upsert is
    idempotent so re-runs and catch-up are safe. Per-fund staleness of the
    freshest returned NAV is logged/alarmed (issue #298) but never changes
    what gets written.

    `fund_codes` restricts the fetch to that subset (confirm-time cold start).
    ``None`` keeps the daily path's full auto-priced fund universe.
    """
    from app.services.fund_nav_fetcher import fetch_nav_history

    selected = _auto_fund_codes(session)
    if fund_codes is not None:
        wanted = set(fund_codes)
        selected = {code: market for code, market in selected.items() if code in wanted}
    if not selected:
        logger.info("capture_fund_navs: no fund_code holdings to capture")
        return 0

    now = datetime.now(tz=UTC)
    rows: list[dict[str, object]] = []

    with httpx.Client() as client:
        for fund_code, market in selected.items():
            nav_history = fetch_nav_history(fund_code, client, lookback_days=lookback_days)
            if nav_history:
                _warn_if_nav_stale(fund_code, nav_history)
            else:
                _warn_if_nav_missing(fund_code)
            for nav_date, nav in nav_history:
                rows.append(
                    {
                        "ticker": fund_code,
                        "market": market,
                        "session_node": "close",
                        "trade_date": nav_date,
                        "close": nav,
                        "captured_at": now,
                    }
                )

    written = _upsert(session, rows)
    logger.info("capture_fund_navs: funds=%d written=%d", len(selected), written)
    return written
