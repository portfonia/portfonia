"""Daily close capture for the three Portfolio Performance benchmark indexes
(issue #360 Phase 1, D9): S&P 500, Dow 30, Nasdaq Composite.

Deliberately does NOT go through `app.services._yfinance.fetch_ohlcv_range` —
that helper's ticker-suffix classification (`_market_key_for_ticker`,
`_fetched_currency`, `_safe_scaled_price`'s GBX detection) exists to resolve
ambiguity across the 7 equity capture markets, none of which applies to a
literal index ticker like `^GSPC`. A small, self-contained `yf.download`
call here avoids feeding an index ticker through machinery built for a
different problem.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.services._yfinance import _quiet_yfinance_logs

logger = logging.getLogger(__name__)

# nasdaq = Nasdaq COMPOSITE (^IXIC), not the Nasdaq-100 (^NDX) — D9, explicit
# because both are common "Nasdaq" shorthands. All three are USD-denominated
# price indexes (no dividend reinvestment).
INDEX_YF_TICKERS: dict[str, str] = {
    "sp500": "^GSPC",
    "dow30": "^DJI",
    "nasdaq": "^IXIC",
}


def _fetch_index_closes(
    yf_tickers: list[str], period: str
) -> dict[str, list[tuple[date, Decimal]]]:
    """{yf_ticker: [(price_date, close), ...]} oldest -> newest, omitting any
    ticker yfinance returned no data for.

    `period` is passed straight to `yf.download` — callers build it (see
    `capture_benchmark_index_prices`/`backfill_benchmark_prices`) rather
    than this function guessing a format from a day/year count. An
    arbitrary `Nd` string does not error for a multi-year N (verified
    against the installed yfinance 1.3.0: it returns N trading-day ROWS,
    which for large N spans MORE calendar time than N days — e.g. `1825d`
    returned 1825 rows spanning ~7.25 calendar years, not 5), but that
    row-count-not-calendar-days semantic is surprising and unrelated to
    what `--years` actually means, so the multi-year backfill path uses
    `Ny` instead (review 5124107298 finding 2 / PR #363) — `Nd` is kept
    only for the short daily catch-up window, which yfinance/this codebase
    already uses this way elsewhere (`_yfinance.fetch_ohlcv_range`).
    """
    if not yf_tickers:
        return {}
    try:
        with _quiet_yfinance_logs():
            hist = yf.download(
                tickers=" ".join(yf_tickers), period=period, auto_adjust=True, progress=False
            )
    except Exception:
        logger.exception("benchmark_prices: yfinance download failed for %s", yf_tickers)
        return {}
    if hist.empty:
        return {}

    out: dict[str, list[tuple[date, Decimal]]] = {}
    close = hist["Close"]
    for yf_ticker in yf_tickers:
        try:
            series = close[yf_ticker] if len(yf_tickers) > 1 else close
        except KeyError:
            continue
        rows: list[tuple[date, Decimal]] = []
        for ts, value in series.items():
            if value != value:  # NaN
                continue
            rows.append((ts.date(), Decimal(str(round(float(value), 4)))))
        if rows:
            out[yf_ticker] = rows
    return out


def _upsert(session: Session, rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    base = pg_insert(BenchmarkPrice).values(rows)
    stmt = base.on_conflict_do_update(
        constraint="uq_benchmark_prices_index_date",
        set_={"close_price": base.excluded.close_price, "currency": base.excluded.currency},
    ).returning(BenchmarkPrice.id)
    return len(session.execute(stmt).fetchall())


def capture_benchmark_index_prices(session: Session, lookback_days: int = 7) -> int:
    """Daily beat entry point: fetch + upsert the latest close(s) for every
    benchmark index. `lookback_days` mirrors `capture_prices`'s catch-up
    window so a missed fire is covered by the next run."""
    yf_to_code = {yf_ticker: code for code, yf_ticker in INDEX_YF_TICKERS.items()}
    period = f"{max(lookback_days, 2)}d"
    fetched = _fetch_index_closes(list(yf_to_code), period=period)
    rows: list[dict[str, object]] = []
    for yf_ticker, points in fetched.items():
        code = yf_to_code[yf_ticker]
        for price_date, close in points:
            rows.append({"index_code": code, "price_date": price_date, "close_price": close})
    written = _upsert(session, rows)
    logger.info(
        "capture_benchmark_index_prices: indexes=%d written=%d", len(INDEX_YF_TICKERS), written
    )
    return written


def backfill_benchmark_prices(session: Session, years: int = 5) -> int:
    """One-off ~`years`-of-history seed — a normal time series fetch, no
    approximation (unlike the portfolio value backfill). Uses yfinance's
    `Ny` period form (not `Nd` — see `_fetch_index_closes`'s docstring)."""
    yf_to_code = {yf_ticker: code for code, yf_ticker in INDEX_YF_TICKERS.items()}
    fetched = _fetch_index_closes(list(yf_to_code), period=f"{max(years, 1)}y")
    rows: list[dict[str, object]] = []
    for yf_ticker, points in fetched.items():
        code = yf_to_code[yf_ticker]
        for price_date, close in points:
            rows.append({"index_code": code, "price_date": price_date, "close_price": close})
    written = _upsert(session, rows)
    print(f"[OK] backfilled {written} benchmark price row(s) across {len(fetched)} index(es)")
    return written


def earliest_benchmark_date(session: Session, index_code: str) -> date | None:
    return session.execute(
        select(BenchmarkPrice.price_date)
        .where(BenchmarkPrice.index_code == index_code)
        .order_by(BenchmarkPrice.price_date.asc())
        .limit(1)
    ).scalar_one_or_none()


def historical_benchmark_price(
    session: Session, index_code: str, as_of_date: date, lookback_days: int = 10
) -> tuple[Decimal, date] | None:
    row = session.execute(
        select(BenchmarkPrice.close_price, BenchmarkPrice.price_date)
        .where(
            BenchmarkPrice.index_code == index_code,
            BenchmarkPrice.price_date <= as_of_date,
            BenchmarkPrice.price_date >= as_of_date - timedelta(days=lookback_days),
        )
        .order_by(BenchmarkPrice.price_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    close, price_date = row
    return (close, price_date)
