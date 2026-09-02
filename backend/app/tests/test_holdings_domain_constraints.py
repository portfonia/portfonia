"""Coverage for the holdings domain CHECK constraints (issue #25).

Two layers, tested separately: Pydantic boundary validation on ParsedRow
(the only user-writable entry point — POST /holdings/confirm takes
list[ParsedRow] directly), and the DB-level CHECK constraints added by
migration 6cd7544f63cf, which guard any write that bypasses the app layer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.schemas.holdings import VALID_CURRENCIES, ParsedRow
from app.services.asset_class_config import VALID_ASSET_CLASSES
from app.tests.conftest import TEST_USER_ID, seed_user


@pytest.fixture(autouse=True)
def _seed_test_user(db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)


def _base_holding(**overrides: object) -> Holding:
    defaults: dict[str, object] = dict(
        user_id=TEST_USER_ID,
        name="Test Holding",
        pricing_mode="auto",
        currency="USD",
        asset_class="STOCK",
        shares=Decimal("10"),
        avg_cost=Decimal("5"),
    )
    defaults.update(overrides)
    return Holding(**defaults)


# --- ParsedRow (app-layer) ---


@pytest.mark.parametrize("field", ["shares", "avg_cost", "current_value"])
def test_parsed_row_rejects_negative_amount(field: str) -> None:
    with pytest.raises(ValidationError):
        ParsedRow(name="X", currency="USD", pricing_mode="auto", **{field: -1})


@pytest.mark.parametrize("field", ["shares", "avg_cost", "current_value"])
def test_parsed_row_accepts_zero_amount(field: str) -> None:
    row = ParsedRow(name="X", currency="USD", pricing_mode="auto", **{field: 0})
    assert getattr(row, field) == 0


def test_parsed_row_rejects_unknown_currency() -> None:
    with pytest.raises(ValidationError):
        ParsedRow(name="X", currency="ZZZ", pricing_mode="auto")


def test_parsed_row_accepts_every_valid_currency() -> None:
    for currency in VALID_CURRENCIES:
        row = ParsedRow(name="X", currency=currency, pricing_mode="auto")
        assert row.currency == currency


def test_parsed_row_accepts_cnh() -> None:
    """Regression (PR #114 review): CNH (offshore yuan) is distinct from CNY
    and already first-class in portfolio_calculator.py's _CURRENCY_TO_FX_PAIR
    and fx_fetcher.py's USDCNH pair — it must not be rejected here."""
    row = ParsedRow(name="X", currency="CNH", pricing_mode="auto")
    assert row.currency == "CNH"


def test_parsed_row_rejects_unknown_asset_class() -> None:
    with pytest.raises(ValidationError):
        ParsedRow(name="X", currency="USD", pricing_mode="auto", asset_class="BOGUS_CLASS")


def test_parsed_row_accepts_every_valid_asset_class() -> None:
    for asset_class in VALID_ASSET_CLASSES:
        row = ParsedRow(name="X", currency="USD", pricing_mode="auto", asset_class=asset_class)
        assert row.asset_class == asset_class


@pytest.mark.parametrize("asset_type", ["cash", "wmf"])
@pytest.mark.parametrize("field,value", [("ticker", "CASH"), ("fund_code", "123456")])
def test_parsed_row_rejects_cash_with_ticker_or_fund_code(
    asset_type: str, field: str, value: str
) -> None:
    """Boundary guard for issue #120: cash/wmf products carry no real
    instrument identifier. _postprocess coerces this away for the normal
    upload path, but POST /holdings/confirm takes list[ParsedRow] directly
    from the client — a caller bypassing _postprocess must get a clean 422,
    not a row that silently misses `current_value` and vanishes from every
    report."""
    with pytest.raises(ValidationError):
        ParsedRow(
            name="X",
            currency="USD",
            pricing_mode="manual",
            asset_type=asset_type,
            current_value=100,
            **{field: value},
        )


@pytest.mark.parametrize("asset_type", ["cash", "wmf"])
def test_parsed_row_rejects_cash_with_amount_only_in_shares(asset_type: str) -> None:
    """Round-2 finding on PR #121: the ticker/fund_code guard alone doesn't
    close the #120 hole. A confirm payload with no ticker but the amount
    still sitting in `shares` (current_value=None) still validates without
    this check, persists, and gets silently dropped from every report by
    compute_portfolio()'s manual-pricing branch, which only ever reads
    current_value."""
    with pytest.raises(ValidationError):
        ParsedRow(
            name="X",
            currency="USD",
            pricing_mode="manual",
            asset_type=asset_type,
            shares=199.98,
            current_value=None,
        )


@pytest.mark.parametrize("asset_type", ["cash", "wmf"])
def test_parsed_row_rejects_cash_with_auto_pricing_mode(asset_type: str) -> None:
    """Round-2 finding on PR #121: cash/wmf has no fetchable price, so
    pricing_mode="auto" can never be valued — compute_portfolio()'s auto
    branch would just add it to stale_tickers too. Must be manual."""
    with pytest.raises(ValidationError):
        ParsedRow(
            name="X",
            currency="USD",
            pricing_mode="auto",
            asset_type=asset_type,
            current_value=100,
        )


@pytest.mark.parametrize("asset_type", ["cash", "wmf"])
def test_parsed_row_accepts_cash_without_ticker_or_fund_code(asset_type: str) -> None:
    row = ParsedRow(
        name="USD Cash",
        currency="USD",
        pricing_mode="manual",
        asset_type=asset_type,
        current_value=100,
    )
    assert row.ticker is None
    assert row.fund_code is None


# --- DB-level CHECK constraints (migration 6cd7544f63cf) ---


def test_db_rejects_unknown_pricing_mode(db_session: Session) -> None:
    db_session.add(_base_holding(pricing_mode="bogus"))
    with pytest.raises(IntegrityError, match="ck_holdings_pricing_mode"):
        db_session.commit()


def test_db_rejects_unknown_asset_type(db_session: Session) -> None:
    db_session.add(_base_holding(asset_type="bogus"))
    with pytest.raises(IntegrityError, match="ck_holdings_asset_type"):
        db_session.commit()


def test_db_allows_null_asset_type(db_session: Session) -> None:
    holding = _base_holding(asset_type=None)
    db_session.add(holding)
    db_session.commit()
    assert holding.asset_type is None


def test_db_rejects_unknown_currency(db_session: Session) -> None:
    db_session.add(_base_holding(currency="ZZZ"))
    with pytest.raises(IntegrityError, match="ck_holdings_currency"):
        db_session.commit()


def test_db_rejects_unknown_asset_class(db_session: Session) -> None:
    db_session.add(_base_holding(asset_class="BOGUS_CLASS"))
    with pytest.raises(IntegrityError, match="ck_holdings_asset_class"):
        db_session.commit()


def test_db_rejects_unknown_market(db_session: Session) -> None:
    db_session.add(_base_holding(market="ASX"))
    with pytest.raises(IntegrityError, match="ck_holdings_market"):
        db_session.commit()


def test_db_allows_null_market(db_session: Session) -> None:
    holding = _base_holding(market=None)
    db_session.add(holding)
    db_session.commit()
    assert holding.market is None


@pytest.mark.parametrize("market", ["UK", "Europe", "Japan", "Korea", "Other"])
def test_db_allows_new_closed_markets(db_session: Session, market: str) -> None:
    holding = _base_holding(market=market)
    db_session.add(holding)
    db_session.commit()
    assert holding.market == market
