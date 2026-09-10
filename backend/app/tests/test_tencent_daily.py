"""Tencent daily-bar parser and Yahoo-adjustment admission (#389)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services._tencent import (
    OhlcBar,
    admit_tencent_missing_dates,
    parse_tencent_daily_payload,
)


def _bar(day: date, close: str, open_: str | None = None) -> OhlcBar:
    o = Decimal(open_ if open_ is not None else close)
    c = Decimal(close)
    return OhlcBar(trade_date=day, open=o, high=max(o, c), low=min(o, c), close=c)


def _payload(
    wire: str, rows: list[list[object]], *, key: str = "day", code: object = 0
) -> dict[str, object]:
    return {"code": code, "data": {wire: {key: rows}}}


SEP4 = date(2026, 9, 4)
SEP7 = date(2026, 9, 7)
SEP8 = date(2026, 9, 8)
WIRE = "sh513500"


def test_parse_reorders_tencent_close_before_high_low() -> None:
    payload = _payload(
        WIRE,
        [["2026-09-07", "2.10", "2.20", "2.30", "2.00", "703305"]],
    )
    bars = parse_tencent_daily_payload(payload, WIRE)
    assert bars[SEP7] == OhlcBar(
        SEP7, Decimal("2.10"), Decimal("2.30"), Decimal("2.00"), Decimal("2.20")
    )


def test_parse_requires_exact_wire_key_and_code_zero() -> None:
    assert (
        parse_tencent_daily_payload(
            _payload("sh513100", [["2026-09-07", "1", "1", "1", "1"]]), WIRE
        )
        == {}
    )
    assert (
        parse_tencent_daily_payload(
            _payload(WIRE, [["2026-09-07", "1", "1", "1", "1"]], code=1), WIRE
        )
        == {}
    )


def test_qfq_accepts_qfqday_or_day_never_hfqday() -> None:
    qfq = parse_tencent_daily_payload(
        _payload(WIRE, [["2026-09-07", "1", "1.1", "1.2", "0.9"]], key="qfqday"),
        WIRE,
        series="qfq",
    )
    day = parse_tencent_daily_payload(
        _payload(WIRE, [["2026-09-07", "1", "1.1", "1.2", "0.9"]], key="day"),
        WIRE,
        series="qfq",
    )
    hfq = parse_tencent_daily_payload(
        {"code": 0, "data": {WIRE: {"hfqday": [["2026-09-07", "1", "1.1", "1.2", "0.9"]]}}},
        WIRE,
        series="qfq",
    )
    assert SEP7 in qfq
    assert SEP7 in day
    assert hfq == {}


def test_parse_rejects_invalid_ohlc_zero_and_conflicting_dates() -> None:
    payload = _payload(
        WIRE,
        [
            ["2026-09-07", "2.20", "2.10", "2.15", "2.00"],  # high 2.15 < max(open,close)
            ["2026-09-08", "0", "1", "1", "1"],
            ["2026-09-04", "1", "1", "1", "1"],
            ["2026-09-04", "2", "2", "2", "2"],
        ],
    )
    assert parse_tencent_daily_payload(payload, WIRE) == {}


def test_clean_window_admits_missing_date_from_raw() -> None:
    yahoo = {SEP4: _bar(SEP4, "2.0000")}
    raw = {SEP4: _bar(SEP4, "2.0000"), SEP7: _bar(SEP7, "2.1000"), SEP8: _bar(SEP8, "2.2000")}
    qfq = dict(raw)
    admitted, reason = admit_tencent_missing_dates(
        missing=(SEP7,),
        yahoo=yahoo,
        raw=raw,
        qfq=qfq,
        expected_sessions=(SEP4, SEP7, SEP8),
    )
    assert reason is None
    assert admitted == {SEP7: raw[SEP7]}


def test_dividend_split_no_anchor_and_missing_qfq_are_unsupported() -> None:
    sessions = (SEP4, SEP7)
    raw = {SEP4: _bar(SEP4, "10"), SEP7: _bar(SEP7, "10")}
    dividend_qfq = {SEP4: _bar(SEP4, "9.5"), SEP7: _bar(SEP7, "9.5")}
    split_qfq = {SEP4: _bar(SEP4, "5"), SEP7: _bar(SEP7, "5")}
    yahoo = {SEP4: _bar(SEP4, "10")}

    admitted, reason = admit_tencent_missing_dates((SEP7,), yahoo, raw, dividend_qfq, sessions)
    assert admitted == {}
    assert reason == "unsupported_adjustment"

    admitted, reason = admit_tencent_missing_dates((SEP7,), yahoo, raw, split_qfq, sessions)
    assert admitted == {}
    assert reason == "unsupported_adjustment"

    admitted, reason = admit_tencent_missing_dates((SEP7,), {}, raw, raw, sessions)
    assert admitted == {}
    assert reason == "unsupported_adjustment"

    qfq_missing = {SEP4: _bar(SEP4, "10")}
    admitted, reason = admit_tencent_missing_dates((SEP7,), yahoo, raw, qfq_missing, sessions)
    assert admitted == {}
    assert reason == "unsupported_adjustment"
