"""Daily close capture for Portfolio Performance benchmark indexes
(issue #360 Phase 1, D9; catalog expansion issue #383): S&P 500, Dow 30,
Nasdaq Composite, CSI 300.

Deliberately does NOT go through `app.services._yfinance.fetch_ohlcv_range` —
that helper's ticker-suffix classification (`_market_key_for_ticker`,
`_fetched_currency`, `_safe_scaled_price`'s GBX detection) exists to resolve
ambiguity across the 7 equity capture markets, none of which applies to a
literal index ticker like `^GSPC` or `000300.SS`. A small, self-contained
`yf.download` call here avoids feeding an index ticker through machinery
built for a different problem.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import httpx
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.benchmark_price import BenchmarkPrice
from app.services._yfinance import _quiet_yfinance_logs

logger = logging.getLogger(__name__)

# nasdaq = Nasdaq COMPOSITE (^IXIC), not the Nasdaq-100 (^NDX) — D9, explicit
# because both are common "Nasdaq" shorthands. csi300 = CSI 300 via the SSE
# listing `000300.SS` (issue #383); do not use `399300.SZ` (same name, no
# multi-year yfinance history). All entries are price indexes (no dividend
# reinvestment). China A50 is deliberately absent — yfinance has no durable
# FTSE China A50 history (see #383 Design).
INDEX_YF_TICKERS: dict[str, str] = {
    "sp500": "^GSPC",
    "dow30": "^DJI",
    "nasdaq": "^IXIC",
    "csi300": "000300.SS",
}

# 000300.SS -> sh000300 (issue #407; same .SS -> sh mapping as #389).
CSI300_YF_TICKER = INDEX_YF_TICKERS["csi300"]
CSI300_TENCENT_SYMBOL = "sh000300"

# Quote currency of the vendor close, stamped on every upsert. The column's
# USD server_default is only a legacy default for pre-#383 US rows — a CNY
# index must never inherit it.
INDEX_CURRENCIES: dict[str, str] = {
    "sp500": "USD",
    "dow30": "USD",
    "nasdaq": "USD",
    "csi300": "CNY",
}

_TENCENT_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_TENCENT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"}
_CSI300_AGREE_ABS = Decimal("0.01")
_CSI300_AGREE_REL = Decimal("0.0001")


def _today() -> date:
    return date.today()


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


def _csi300_closes_agree(tencent: Decimal, yahoo: Decimal) -> bool:
    return abs(tencent - yahoo) <= max(_CSI300_AGREE_ABS, abs(yahoo) * _CSI300_AGREE_REL)


def _parse_tencent_csi300_day_bars(payload: object) -> dict[date, Decimal]:
    """Tencent bars are [date, open, close, high, low, ...], not Yahoo OHLC."""
    if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    block = data.get(CSI300_TENCENT_SYMBOL)
    if not isinstance(block, dict):
        return {}
    day = block.get("day")
    if not isinstance(day, list):
        return {}
    out: dict[date, Decimal] = {}
    for row in day:
        if not isinstance(row, list) or len(row) < 3:
            continue
        try:
            price_date = date.fromisoformat(str(row[0])[:10])
            close = Decimal(str(row[2]))
        except (TypeError, ValueError, InvalidOperation, ArithmeticError):
            continue
        if close.is_finite() and close > 0:
            out[price_date] = close
    return out


def _fetch_tencent_csi300_closes(start: date, end: date) -> dict[date, Decimal]:
    if end < start:
        return {}
    n = max((end - start).days + 8, 2)
    param = f"{CSI300_TENCENT_SYMBOL},day,{start.isoformat()},{end.isoformat()},{n},"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(_TENCENT_KLINE_URL, params={"param": param}, headers=_TENCENT_HEADERS)
            resp.raise_for_status()
            payload: object = resp.json()
    except Exception:
        logger.exception("csi300 tencent kline fetch failed")
        return {}
    parsed = _parse_tencent_csi300_day_bars(payload)
    return {d: close for d, close in parsed.items() if start <= d <= end}


def _csi300_fallback_points(
    yahoo_points: list[tuple[date, Decimal]], window_start: date, window_end: date
) -> list[tuple[date, Decimal]]:
    """Fill weekday holes in the Yahoo csi300 series from Tencent, or [].

    Requires a same-chain Yahoo bar adjacent to the hole whose close agrees
    with Tencent. No schema change: provenance is the log line.
    """
    if not yahoo_points:
        logger.info("csi300 fallback unsupported: no Yahoo anchor")
        return []
    yahoo = {d: close for d, close in yahoo_points}
    start = max(window_start, min(yahoo))
    missing: list[date] = []
    cursor = start
    while cursor <= window_end:
        if cursor.weekday() < 5 and cursor not in yahoo:
            missing.append(cursor)
        cursor += timedelta(days=1)
    if not missing:
        return []
    left = max((d for d in yahoo if d < missing[0]), default=None)
    right = min((d for d in yahoo if d > missing[-1]), default=None)
    if left is None and right is None:
        logger.info("csi300 fallback unsupported: no Yahoo anchor")
        return []
    tencent = _fetch_tencent_csi300_closes(left or missing[0], right or missing[-1])
    agreed = False
    for anchor in (left, right):
        if anchor is None or anchor not in tencent:
            continue
        if not _csi300_closes_agree(tencent[anchor], yahoo[anchor]):
            logger.info("csi300 fallback rejected: anchor disagreement on %s", anchor.isoformat())
            return []
        agreed = True
    if not agreed:
        logger.info("csi300 fallback unsupported: Tencent missing Yahoo anchor")
        return []
    filled = [(d, tencent[d]) for d in missing if d in tencent]
    if filled:
        logger.info("csi300 tencent fallback admitted=%d", len(filled))
    return filled


# PostgreSQL/psycopg hard-cap a single query at 65535 bound parameters.
# Benchmark rows bind 4 params each; 2000 leaves the same safety margin
# price_capture.py's precedent (issue #194) uses for its 10-column rows.
_UPSERT_CHUNK_SIZE = 2000


def _upsert(session: Session, rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), _UPSERT_CHUNK_SIZE):
        written += _upsert_chunk(session, rows[start : start + _UPSERT_CHUNK_SIZE])
    return written


def _upsert_chunk(session: Session, rows: list[dict[str, object]]) -> int:
    base = pg_insert(BenchmarkPrice).values(rows)
    stmt = base.on_conflict_do_update(
        constraint="uq_benchmark_prices_index_date",
        set_={"close_price": base.excluded.close_price, "currency": base.excluded.currency},
    ).returning(BenchmarkPrice.id)
    return len(session.execute(stmt).fetchall())


def _append_csi300_fallback(
    rows: list[dict[str, object]],
    fetched: dict[str, list[tuple[date, Decimal]]],
    window_start: date,
    window_end: date,
) -> None:
    for price_date, close in _csi300_fallback_points(
        fetched.get(CSI300_YF_TICKER, []), window_start, window_end
    ):
        rows.append(
            {
                "index_code": "csi300",
                "price_date": price_date,
                "close_price": close,
                "currency": INDEX_CURRENCIES["csi300"],
            }
        )


def capture_benchmark_index_prices(session: Session, lookback_days: int = 7) -> int:
    """Daily beat entry point: fetch + upsert the latest close(s) for every
    benchmark index. `lookback_days` mirrors `capture_prices`'s catch-up
    window so a missed fire is covered by the next run."""
    yf_to_code = {yf_ticker: code for code, yf_ticker in INDEX_YF_TICKERS.items()}
    period = f"{max(lookback_days, 2)}d"
    window_end = _today()
    window_start = window_end - timedelta(days=max(lookback_days, 2))
    fetched = _fetch_index_closes(list(yf_to_code), period=period)
    rows: list[dict[str, object]] = []
    for yf_ticker, points in fetched.items():
        code = yf_to_code[yf_ticker]
        currency = INDEX_CURRENCIES[code]
        for price_date, close in points:
            rows.append(
                {
                    "index_code": code,
                    "price_date": price_date,
                    "close_price": close,
                    "currency": currency,
                }
            )
    _append_csi300_fallback(rows, fetched, window_start, window_end)
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
    window_end = _today()
    window_start = window_end - timedelta(days=365 * max(years, 1))
    fetched = _fetch_index_closes(list(yf_to_code), period=f"{max(years, 1)}y")
    rows: list[dict[str, object]] = []
    for yf_ticker, points in fetched.items():
        code = yf_to_code[yf_ticker]
        currency = INDEX_CURRENCIES[code]
        for price_date, close in points:
            rows.append(
                {
                    "index_code": code,
                    "price_date": price_date,
                    "close_price": close,
                    "currency": currency,
                }
            )
    _append_csi300_fallback(rows, fetched, window_start, window_end)
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
