"""Tests for price_anomaly_detector (E3).

Holdings tests mock fetch_last_two_closes (no real yfinance calls).
FX tests use a real Postgres session (db_session fixture) to verify
the DB-query path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.services import price_anomaly_detector as pad

_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)
_PREV = datetime(2026, 6, 3, 20, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _holding(
    name: str,
    ticker: str,
    asset_type: str = "stock",
    pricing_mode: str = "auto",
) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        pricing_mode=pricing_mode,
        ticker=ticker,
        currency="USD",
        shares=Decimal("10"),
        asset_type=asset_type,
    )


def _fx(pair: str, rate: float, rate_date: date) -> FxRate:
    return FxRate(pair=pair, rate=Decimal(str(rate)), rate_date=rate_date)


# ---------------------------------------------------------------------------
# Holdings anomaly detection
# ---------------------------------------------------------------------------


def test_stock_above_threshold_is_anomaly(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()

    # +5% move, threshold is ±3%
    two_closes = {"AAPL": ((105.0, _NOW), (100.0, _PREV))}

    with patch.object(pad, "fetch_last_two_closes", return_value=two_closes):
        result = pad.detect_price_anomalies(db_session)

    assert len(result) == 1
    a = result[0]
    assert a.identifier == "AAPL"
    assert a.asset_type == "stock"
    assert a.pct_change == Decimal("0.0500")
    assert a.threshold == Decimal("0.03")


def test_stock_below_threshold_not_anomaly(db_session: Session) -> None:
    db_session.add(_holding("Apple", "AAPL"))
    db_session.flush()

    # +1% move, below ±3% threshold
    two_closes = {"AAPL": ((101.0, _NOW), (100.0, _PREV))}
    with patch.object(pad, "fetch_last_two_closes", return_value=two_closes):
        result = pad.detect_price_anomalies(db_session)

    assert result == []


def test_etf_uses_lower_threshold(db_session: Session) -> None:
    db_session.add(_holding("VOO", "VOO", asset_type="etf"))
    db_session.flush()

    # +2.5% move: above ETF ±2%, below stock ±3%
    two_closes = {"VOO": ((102.5, _NOW), (100.0, _PREV))}
    with patch.object(pad, "fetch_last_two_closes", return_value=two_closes):
        result = pad.detect_price_anomalies(db_session)

    assert len(result) == 1
    assert result[0].threshold == Decimal("0.02")


def test_negative_move_detected(db_session: Session) -> None:
    db_session.add(_holding("Intel", "INTC"))
    db_session.flush()

    # -4% move
    two_closes = {"INTC": ((96.0, _NOW), (100.0, _PREV))}
    with patch.object(pad, "fetch_last_two_closes", return_value=two_closes):
        result = pad.detect_price_anomalies(db_session)

    assert len(result) == 1
    assert result[0].pct_change == Decimal("-0.0400")


def test_no_prev_close_skipped(db_session: Session) -> None:
    db_session.add(_holding("NewCo", "NEW"))
    db_session.flush()

    two_closes = {"NEW": ((100.0, _NOW), None)}
    with patch.object(pad, "fetch_last_two_closes", return_value=two_closes):
        result = pad.detect_price_anomalies(db_session)

    assert result == []


def test_no_price_data_skipped(db_session: Session) -> None:
    db_session.add(_holding("Ghost", "GHOST"))
    db_session.flush()

    with patch.object(pad, "fetch_last_two_closes", return_value={}):
        result = pad.detect_price_anomalies(db_session)

    assert result == []


def test_manual_mode_holding_skipped(db_session: Session) -> None:
    db_session.add(_holding("Manual", "MANUAL", pricing_mode="manual"))
    db_session.flush()

    with patch.object(pad, "fetch_last_two_closes", return_value={}) as mock_fetch:
        result = pad.detect_price_anomalies(db_session)

    mock_fetch.assert_not_called()
    assert result == []


def test_fund_holding_skipped(db_session: Session) -> None:
    """Funds have no ticker-based daily price; should not trigger anomaly check."""
    h = Holding(
        user_id=_USER,
        name="EF Fund",
        pricing_mode="auto",
        fund_code="110011",
        currency="CNY",
        shares=Decimal("100"),
        asset_type="fund",
    )
    db_session.add(h)
    db_session.flush()

    with patch.object(pad, "fetch_last_two_closes", return_value={}) as mock_fetch:
        pad.detect_price_anomalies(db_session)

    mock_fetch.assert_not_called()


def test_sorted_by_abs_pct_change_descending(db_session: Session) -> None:
    db_session.add_all(
        [
            _holding("Big Move", "BIG"),
            _holding("Small Move", "SML"),
        ]
    )
    db_session.flush()

    two_closes = {
        "BIG": ((110.0, _NOW), (100.0, _PREV)),  # +10%
        "SML": ((103.5, _NOW), (100.0, _PREV)),  # +3.5%
    }
    with patch.object(pad, "fetch_last_two_closes", return_value=two_closes):
        result = pad.detect_price_anomalies(db_session)

    assert result[0].identifier == "BIG"
    assert result[1].identifier == "SML"


# ---------------------------------------------------------------------------
# FX anomaly detection (uses real DB)
# ---------------------------------------------------------------------------


def test_fx_above_threshold_is_anomaly(db_session: Session) -> None:
    today = date(2026, 6, 4)
    yesterday = date(2026, 6, 3)
    # +1.5% move on USDCNY: above ±1% threshold
    db_session.add_all(
        [
            _fx("USDCNY", 7.25, today),
            _fx("USDCNY", 7.14, yesterday),
        ]
    )
    db_session.flush()

    with patch.object(pad, "fetch_last_two_closes", return_value={}):
        result = pad.detect_price_anomalies(db_session)

    fx_hits = [a for a in result if a.asset_type == "fx"]
    assert len(fx_hits) == 1
    assert fx_hits[0].identifier == "USDCNY"
    assert fx_hits[0].threshold == Decimal("0.01")


def test_fx_below_threshold_not_anomaly(db_session: Session) -> None:
    today = date(2026, 6, 4)
    yesterday = date(2026, 6, 3)
    # +0.5% move: below ±1%
    db_session.add_all(
        [
            _fx("USDHKD", 7.83, today),
            _fx("USDHKD", 7.79, yesterday),
        ]
    )
    db_session.flush()

    with patch.object(pad, "fetch_last_two_closes", return_value={}):
        result = pad.detect_price_anomalies(db_session)

    assert not any(a.identifier == "USDHKD" for a in result)


def test_fx_only_one_date_no_anomaly(db_session: Session) -> None:
    db_session.add(_fx("USDCNY", 7.25, date(2026, 6, 4)))
    db_session.flush()

    with patch.object(pad, "fetch_last_two_closes", return_value={}):
        result = pad.detect_price_anomalies(db_session)

    assert result == []
