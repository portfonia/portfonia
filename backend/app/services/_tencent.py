"""Tencent daily bars for eligible China ETF close gaps (issue #389).

This is not the csi300 benchmark fallback in ``benchmark_prices``: different
instrument, table, and admission rule. Raw and qfq series are fetched over
identical frozen bounds; a missing date is written only after a same-chain
Yahoo adjusted bar agrees with Tencent raw, and raw/qfq agree across every
completed session in the interval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

import httpx

from app.services.capture_results import is_positive_finite, prices_agree

logger = logging.getLogger(__name__)

_TENCENT_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_TENCENT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"}
_TIMEOUT_SECONDS = 10.0
TencentSeries = Literal["raw", "qfq"]


@dataclass(frozen=True)
class OhlcBar:
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def _parse_decimal(raw: object) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw).strip())
    except Exception:
        return None
    if not is_positive_finite(value):
        return None
    return value


def _valid_ohlc(open_: Decimal, high: Decimal, low: Decimal, close: Decimal) -> bool:
    return low <= min(open_, close) <= max(open_, close) <= high


def _bar_from_row(row: object) -> OhlcBar | None:
    if not isinstance(row, list) or len(row) < 5:
        return None
    try:
        trade_date = date.fromisoformat(str(row[0])[:10])
    except ValueError:
        return None
    open_ = _parse_decimal(row[1])
    close = _parse_decimal(row[2])
    high = _parse_decimal(row[3])
    low = _parse_decimal(row[4])
    if open_ is None or close is None or high is None or low is None:
        return None
    if not _valid_ohlc(open_, high, low, close):
        return None
    return OhlcBar(trade_date=trade_date, open=open_, high=high, low=low, close=close)


def parse_tencent_daily_payload(
    payload: object, wire_symbol: str, series: TencentSeries = "raw"
) -> dict[date, OhlcBar]:
    if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    block = data.get(wire_symbol)
    if not isinstance(block, dict):
        return {}
    rows: object
    if series == "qfq":
        if "qfqday" in block:
            rows = block.get("qfqday")
        elif "day" in block:
            rows = block.get("day")
        else:
            return {}
    else:
        rows = block.get("day")
    if not isinstance(rows, list):
        return {}

    by_date: dict[date, OhlcBar | None] = {}
    for row in rows:
        bar = _bar_from_row(row)
        if bar is None:
            continue
        if bar.trade_date in by_date:
            previous = by_date[bar.trade_date]
            if previous is None or previous != bar:
                by_date[bar.trade_date] = None
            continue
        by_date[bar.trade_date] = bar
    return {day: bar for day, bar in by_date.items() if bar is not None}


def fetch_tencent_daily_bars(
    wire_symbol: str,
    start: date,
    end: date,
    series: TencentSeries,
) -> dict[date, OhlcBar]:
    """One raw or qfq request. No internal retry/sleep. Timeout 10 seconds."""
    if end < start:
        return {}
    n = (end - start).days + 5
    suffix = "qfq" if series == "qfq" else ""
    param = f"{wire_symbol},day,{start.isoformat()},{end.isoformat()},{n},{suffix}"
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(
                _TENCENT_KLINE_URL,
                params={"param": param},
                headers=_TENCENT_HEADERS,
            )
            resp.raise_for_status()
            payload: object = resp.json()
    except Exception:
        logger.exception("tencent %s kline fetch failed for %s", series, wire_symbol)
        return {}
    parsed = parse_tencent_daily_payload(payload, wire_symbol, series=series)
    return {day: bar for day, bar in parsed.items() if start <= day <= end}


def ohlc_agree(left: OhlcBar, right: OhlcBar) -> bool:
    return (
        prices_agree(left.open, right.open)
        and prices_agree(left.high, right.high)
        and prices_agree(left.low, right.low)
        and prices_agree(left.close, right.close)
    )


def _interval_ok(
    anchor: date,
    missing: date,
    raw: dict[date, OhlcBar],
    qfq: dict[date, OhlcBar],
    expected_sessions: tuple[date, ...],
) -> bool:
    lo = min(anchor, missing)
    hi = max(anchor, missing)
    needed = tuple(day for day in expected_sessions if lo <= day <= hi)
    if not needed:
        return False
    for day in needed:
        raw_bar = raw.get(day)
        qfq_bar = qfq.get(day)
        if raw_bar is None or qfq_bar is None:
            return False
        if not ohlc_agree(raw_bar, qfq_bar):
            return False
    return True


def admit_tencent_missing_dates(
    missing: tuple[date, ...],
    yahoo: dict[date, OhlcBar],
    raw: dict[date, OhlcBar],
    qfq: dict[date, OhlcBar],
    expected_sessions: tuple[date, ...],
) -> tuple[dict[date, OhlcBar], str | None]:
    """Admit raw bars only for no-adjustment-compatible intervals.

    Returns ``(admitted, None)`` or ``({}, "unsupported_adjustment")``.
    """
    if not missing:
        return {}, None
    if not yahoo:
        return {}, "unsupported_adjustment"
    admitted: dict[date, OhlcBar] = {}
    for day in missing:
        raw_bar = raw.get(day)
        qfq_bar = qfq.get(day)
        if raw_bar is None or qfq_bar is None:
            return {}, "unsupported_adjustment"
        anchors = sorted(yahoo, key=lambda anchor: (abs((anchor - day).days), anchor))
        accepted = False
        for anchor in anchors:
            yahoo_bar = yahoo[anchor]
            tencent_anchor = raw.get(anchor)
            if tencent_anchor is None:
                continue
            if not ohlc_agree(tencent_anchor, yahoo_bar):
                continue
            if not _interval_ok(anchor, day, raw, qfq, expected_sessions):
                continue
            accepted = True
            break
        if not accepted:
            return {}, "unsupported_adjustment"
        admitted[day] = raw_bar
    return admitted, None
