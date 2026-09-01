"""Capture price snapshots into `price_snapshots` (ADR-002 capture layer).

Credit-free (yfinance). The `close` node stores the authoritative daily OHLCV
bar; intraday nodes (pre_open / open / after_close) store a best-effort `last`
(null when yfinance has no intraday value). Idempotent upsert on
(ticker, market, session_node, trade_date) so catch-up re-runs overwrite rather
than duplicate. FX is NOT captured here — it stays in `fx_rates`.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

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


# Issue #298: a healthy fund publishes same-evening NAV, so a latest NAV more
# than 2 calendar days behind today (CST) means the fund has not progressed.
_FUND_NAV_STALE_DAYS = 2


def _warn_if_nav_stale(fund_code: str, nav_history: list[tuple[date, Decimal]]) -> None:
    """Log a per-fund WARNING and ops-alert when the latest NAV is stale.

    Observability only (issue #298): does not alter capture behavior. Expects
    `nav_history` sorted ascending (fetch_nav_history's contract), so the last
    entry is the freshest. The alert is idempotent per fund_code + NAV date so
    a persisting gap does not re-alert on every daily run within the dedup
    window.
    """
    if not nav_history:
        return
    latest_nav_date = nav_history[-1][0]
    lag_days = (_cst_today() - latest_nav_date).days
    if lag_days <= _FUND_NAV_STALE_DAYS:
        return
    logger.warning(
        "capture_fund_navs: fund %s latest NAV %s lags today (CST) by %d calendar days",
        fund_code,
        latest_nav_date.isoformat(),
        lag_days,
    )
    send_ops_alert(
        subject=f"[Portfonia] fund NAV stale — {fund_code}",
        body=(
            f"capture_fund_navs_task found fund {fund_code} with latest NAV dated "
            f"{latest_nav_date.isoformat()}, which is {lag_days} calendar days behind "
            f"today (CST).\n\n"
            f"Healthy funds in the same run capture a same-day or 1-day-old NAV; this "
            f"fund has not progressed. The gap may be a late evening NAV publish or a "
            f"capture-task under-delivery (issue #135).\n\n"
            f"Check price_snapshots and worker.log for capture_fund_navs_task runs "
            f"mentioning this code."
        ),
        idempotency_key=f"ops-fund-nav-stale-{fund_code}-{latest_nav_date.isoformat()}",
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
            _warn_if_nav_stale(fund_code, nav_history)
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
