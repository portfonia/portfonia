"""Integration tests for portfolio_calculator — real Postgres, no network."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
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
    asset_class: str = "STOCK",
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
        asset_class=asset_class,
        sector=sector,
    )


def _cash(name: str, currency: str, value: str, *, asset_class: str = "CASH_EQUIV") -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        pricing_mode="manual",
        currency=currency,
        current_value=Decimal(value),
        asset_type="cash",
        asset_class=asset_class,
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

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

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
    # asset_class has no "Other" fallback — every holding (incl. cash) lands
    # in a real bucket, this is what §1/distribution/§4.1 read.
    assert snap.by_asset_class == {
        "STOCK": Decimal("5000.00"),
        "CASH_EQUIV": Decimal("5000.00"),
    }


def test_declared_market_overrides_derivation(db_session: Session) -> None:
    """A user-declared market (e.g. USD cash in an IBKR/US account) wins over
    the ticker-derived bucket — cash must land in US, not the 'Other' default."""
    _seed_fx(db_session)
    cash = _cash("USD Cash", "USD", "5000")
    cash.market = "US"  # declared via the .md market column
    db_session.add_all([_stock("Apple", "AAPL", "USD", "10", "300"), cash])
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.by_market == {"US": Decimal("8000.00")}
    assert {hv.name: hv.market for hv in snap.holdings}["USD Cash"] == "US"


def test_concentration_flags(db_session: Session) -> None:
    """Single-holding thresholds depend on the top holding's own asset_class
    (STOCK is tight: >10%/>20%); the asset_class bucket (not raw per-row
    ranking) drives the top-asset-class check, with no "Other" fallback."""
    _seed_fx(db_session)
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300", sector="Technology"),
            _stock("Moutai", "600519.SS", "CNY", "10", "1400", sector="Consumer Staples"),
            _cash("USD Cash", "USD", "1000"),
        ]
    )
    db_session.flush()

    c = compute_portfolio(db_session, user_id=_USER, base_currency="USD").concentration

    assert c.top_holding_name == "Apple"  # 3000 > 2000 > 1000
    assert c.top_holding_ratio == Decimal("0.5000")  # 3000 / 6000
    assert c.top_holding_asset_class == "STOCK"
    assert c.single_holding_watch is True  # 0.50 > STOCK watch 0.10
    assert c.single_holding_high is True  # 0.50 > STOCK high 0.20
    assert c.top3_ratio == Decimal("1.0000")
    assert c.top3_watch is True  # >0.50
    assert c.top_asset_class_name == "STOCK"  # Apple + Moutai = 5000 > cash 1000
    assert c.top_asset_class_ratio == Decimal("0.8333")  # 5000 / 6000
    assert c.asset_class_watch is True  # 0.8333 > 0.50
    assert c.asset_class_high is True  # 0.8333 > 0.65


def test_concentration_thresholds_loosen_for_broad_index_top_holding(db_session: Session) -> None:
    """A broad-index ETF carries a wider single-holding threshold than a
    single stock at the same weight, since it is already diversified."""
    _seed_fx(db_session)
    db_session.add_all(
        [
            _stock(
                "Vanguard S&P 500",
                "VOO",
                "USD",
                "10",
                "300",
                asset_class="EQUITY_US_BROAD",
            ),
            _cash("USD Cash", "USD", "3000"),
        ]
    )
    db_session.flush()

    c = compute_portfolio(db_session, user_id=_USER, base_currency="USD").concentration

    assert c.top_holding_name == "Vanguard S&P 500"
    assert c.top_holding_ratio == Decimal("0.5000")
    assert c.top_holding_asset_class == "EQUITY_US_BROAD"
    assert c.single_holding_watch is True  # 0.50 > EQUITY_US_BROAD watch 0.30
    assert c.single_holding_high is True  # 0.50 > EQUITY_US_BROAD high 0.45


def test_unclassified_stock_sector_defaults_to_other(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add(_stock("HK Co", "0700.HK", "HKD", "100", "80", sector=None))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert "Other" in snap.by_sector
    assert snap.by_sector["Other"] == Decimal("1000.00")  # 100*80 HKD / 8


def test_base_currency_cny(db_session: Session) -> None:
    _seed_fx(db_session)
    db_session.add(_stock("Apple", "AAPL", "USD", "10", "300", sector="Technology"))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="CNY")

    assert snap.total_base == Decimal("21000.00")  # 3000 USD * 7.0


def test_missing_fx_rate_marks_stale(db_session: Session) -> None:
    # No USDHKD seeded → HKD holding cannot convert.
    db_session.add(FxRate(pair="USDCNY", rate=Decimal("7.0"), rate_date=_FX_DATE, source="test"))
    db_session.add(_stock("HK Co", "0700.HK", "HKD", "100", "80"))
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.total_base == Decimal("0")
    assert "0700.HK" in snap.stale_tickers


def test_empty_portfolio_has_no_concentration(db_session: Session) -> None:
    _seed_fx(db_session)
    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")
    assert snap.total_base == Decimal("0")
    assert snap.concentration.top_holding_name is None


def test_user_isolation(db_session: Session) -> None:
    """compute_portfolio must not include holdings belonging to another user."""
    _seed_fx(db_session)
    other_user = uuid.UUID("00000000-0000-0000-0000-000000000002")
    db_session.add_all(
        [
            _stock("Apple", "AAPL", "USD", "10", "300"),  # belongs to _USER
            Holding(
                user_id=other_user,
                name="Other User Stock",
                pricing_mode="auto",
                ticker="MSFT",
                currency="USD",
                shares=Decimal("10"),
                market_price=Decimal("400"),
                asset_type="stock",
                asset_class="STOCK",
            ),
        ]
    )
    db_session.flush()

    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD")

    assert snap.total_base == Decimal("3000.00")
    assert all(hv.ticker != "MSFT" for hv in snap.holdings)


def test_stale_priced_ticker_flagged(db_session: Session) -> None:
    """A captured close older than 4 calendar days lands in stale_priced_tickers,
    not stale_tickers — it is still included in totals."""
    _seed_fx(db_session)
    holding = _stock("Apple", "AAPL", "USD", "10", None)  # no fallback market_price
    db_session.add(holding)
    db_session.flush()

    stale_date = date(2026, 1, 2)  # 10 days before as_of
    db_session.add(
        PriceSnapshot(
            ticker="AAPL",
            market="US",
            session_node="close",
            trade_date=stale_date,
            close=Decimal("300"),
        )
    )
    db_session.flush()

    as_of = date(2026, 1, 12)
    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD", as_of=as_of)

    assert snap.stale_priced_tickers == ["AAPL"]
    assert snap.stale_tickers == []
    assert snap.total_base == Decimal("3000.00")  # still included


def test_fresh_price_not_flagged(db_session: Session) -> None:
    """A captured close within 4 calendar days is not flagged as stale."""
    _seed_fx(db_session)
    holding = _stock("Apple", "AAPL", "USD", "10", None)
    db_session.add(holding)
    db_session.flush()

    trade_date = date(2026, 1, 9)  # 3 days before as_of
    db_session.add(
        PriceSnapshot(
            ticker="AAPL",
            market="US",
            session_node="close",
            trade_date=trade_date,
            close=Decimal("300"),
        )
    )
    db_session.flush()

    as_of = date(2026, 1, 12)
    snap = compute_portfolio(db_session, user_id=_USER, base_currency="USD", as_of=as_of)

    assert snap.stale_priced_tickers == []
    assert snap.stale_tickers == []
    assert snap.total_base == Decimal("3000.00")


def test_to_base_cross_currency() -> None:
    fx = {"USDHKD": Decimal("8.0"), "USDCNY": Decimal("7.0")}
    # 80 HKD → 10 USD → 70 CNY
    assert _to_base(Decimal("80"), "HKD", "CNY", fx) == Decimal("70")
    # same currency is identity
    assert _to_base(Decimal("42"), "USD", "USD", fx) == Decimal("42")
    # missing pair → None
    assert _to_base(Decimal("1"), "JPY", "USD", fx) is None
