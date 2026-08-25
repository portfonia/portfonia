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

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.timezones import MARKET_TZ
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.services._yfinance import _market_key_for_ticker, fetch_ohlcv_range, fetch_spot

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
    out: dict[str, str] = {}
    for h in holdings:
        code = h.fund_code
        if code and code not in out:
            out[code] = h.market or "A-Share"
    return out


def capture_fund_navs(
    session: Session,
    lookback_days: int = 30,
    fund_codes: list[str] | None = None,
) -> int:
    """Fetch settled NAV history from Tiantian Fund for fund_code holdings.

    Upserts into price_snapshots using fund_code as ticker key, market from the
    holding (defaulting to A-Share), session_node='close'. The upsert is
    idempotent so re-runs and catch-up are safe.

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
