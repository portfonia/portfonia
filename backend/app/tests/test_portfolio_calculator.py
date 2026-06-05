"""Integration tests for portfolio_calculator — real Postgres, no network."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.services.portfolio_calculator import _to_base, compute_portfolio

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
_FX_DATE = date(2026, 1, 2)


def _seed_fx(session: Session) -> None:
    session.add_all(
        [
            FxRate(pair="USDCNY", rate=Decimal("7.0"), rate_date=_FX_DATE, source="test"),
            FxRate(pair="USDHKD", rate=Decimal("8.0"), rate_date=_FX_DATE, source="test"),
            FxRate(pair="USDCNH", rate=Decimal("7.1"), rate_date=_FX_DATE, source="test"),
        ]
    )


def _stock(
    name: str,
    ticker: str | None,
    currency: str,
    shares: str | None,
    price: str | None,
    *,
    asset_type: str = "stock",
    sector: str | None = None,
    fund_code: str | None = None,
) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        pricing_mode="auto",
        ticker=ticker,
        fund_code=fund_code,
        currency=currency,
        shares=Decimal(shares) if shares is not None else None,
        market_price=Decimal(price) if price is not None else None,
        asset_type=asset_type,
        sector=sector,
    )


def _cash(name: str, currency: str, value: str) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        pricing_mode="manual",
        currency=currency,
        current_value=Decimal(value),
        asset_type="cash",
    )


def test_full_snapshot_values_and_distributions(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300", sector="Technology"),
            _stock("Moutai", "600519.SS", "CNY", "10", "1400", sector="Consumer Staples"),
            _cash("USD Cash", "USD", "5000"),
            _stock("Broken", "BAD", "USD", None, None),  # missing price → stale
        ]
    )
    db_session.flush()

    snap = compute_portfolio(db_session, base_currency="USD")

    assert snap.fx_date == _FX_DATE
    assert snap.stale_tickers == ["BAD"]
    # 3000 USD + 14000 CNY/7 = 2000 USD + 5000 cash
    assert snap.total_base == Decimal("10000.00")
    assert snap.by_currency == {"USD": Decimal("8000.00"), "CNY": Decimal("2000.00")}
    assert snap.by_asset_type == {"stock": Decimal("5000.00"), "cash": Decimal("5000.00")}
    assert snap.by_market == {
        "US": Decimal("3000.00"),
        "A-Share": Decimal("2000.00"),
        "Other": Decimal("5000.00"),
    }
    # cash is excluded from the equity sector chart
    assert snap.by_sector == {
        "Technology": Decimal("3000.00"),
        "Consumer Staples": Decimal("2000.00"),
    }


def test_concentration_flags(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300", sector="Technology"),
            _stock("Moutai", "600519.SS", "CNY", "10", "1400", sector="Consumer Staples"),
            _cash("USD Cash", "USD", "5000"),
        ]
    )
    db_session.flush()

    c = compute_portfolio(db_session, base_currency="USD").concentration

    assert c.top_holding_name == "USD Cash"
    assert c.top_holding_ratio == Decimal("0.5000")
    assert c.single_holding_watch is True  # >0.15
    assert c.single_holding_high is True  # >0.25
    assert c.top3_ratio == Decimal("1.0000")
    assert c.top3_watch is True  # >0.50
    assert c.top_sector_name == "Technology"  # 3000 > 2000
    assert c.top_sector_ratio == Decimal("0.3000")
    assert c.sector_watch is False  # 0.30 not > 0.35


def test_unclassified_stock_sector_defaults_to_other(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add(_stock("HK Co", "0700.HK", "HKD", "100", "80", sector=None))
    db_session.flush()

    snap = compute_portfolio(db_session, base_currency="USD")

    assert "Other" in snap.by_sector
    assert snap.by_sector["Other"] == Decimal("1000.00")  # 100*80 HKD / 8


def test_base_currency_cny(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add(_stock("Apple", "AAPL", "USD", "10", "300", sector="Technology"))
    db_session.flush()

    snap = compute_portfolio(db_session, base_currency="CNY")

    assert snap.total_base == Decimal("21000.00")  # 3000 USD * 7.0


def test_missing_fx_rate_marks_stale(db_session: Session) -> None:
    # No USDHKD seeded → HKD holding cannot convert.
    db_session.add(FxRate(pair="USDCNY", rate=Decimal("7.0"), rate_date=_FX_DATE, source="test"))
    db_session.add(_stock("HK Co", "0700.HK", "HKD", "100", "80"))
    db_session.flush()

    snap = compute_portfolio(db_session, base_currency="USD")

    assert snap.total_base == Decimal("0")
    assert "0700.HK" in snap.stale_tickers


def test_empty_portfolio_has_no_concentration(db_session: Session) -> None:
    _seed_fx(db_session)
    snap = compute_portfolio(db_session, base_currency="USD")
    assert snap.total_base == Decimal("0")
    assert snap.concentration.top_holding_name is None


def test_to_base_cross_currency() -> None:
    fx = {"USDHKD": Decimal("8.0"), "USDCNY": Decimal("7.0")}
    # 80 HKD → 10 USD → 70 CNY
    assert _to_base(Decimal("80"), "HKD", "CNY", fx) == Decimal("70")
    # same currency is identity
    assert _to_base(Decimal("42"), "USD", "USD", fx) == Decimal("42")
    # missing pair → None
    assert _to_base(Decimal("1"), "JPY", "USD", fx) is None
