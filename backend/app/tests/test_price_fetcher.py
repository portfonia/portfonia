"""Integration tests for price_fetcher — real Postgres, mocked yfinance."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.services import price_fetcher

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
_AS_OF = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)


def _auto(name: str, ticker: str, *, asset_type: str = "stock") -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        pricing_mode="auto",
        ticker=ticker,
        currency="USD",
        shares=Decimal("10"),
        asset_type=asset_type,
    )


def test_prices_written_to_db(db_session: Session) -> None:
    db_session.add_all([_auto("Apple", "AAPL"), _auto("Msft", "MSFT")])
    db_session.flush()

    points = {"AAPL": (310.0, _AS_OF), "MSFT": (420.0, _AS_OF)}
    with patch.object(price_fetcher, "fetch_last_close", return_value=points):
        result = price_fetcher.update_holding_prices(db_session)

    assert result.updated == 2
    assert result.failed == []

    rows = {h.ticker: h for h in db_session.query(Holding).all()}
    assert rows["AAPL"].market_price == Decimal("310.0")
    assert rows["AAPL"].price_as_of == _AS_OF
    assert rows["AAPL"].price_fetched_at is not None


def test_missing_ticker_marked_failed_not_fatal(db_session: Session) -> None:
    db_session.add_all([_auto("Apple", "AAPL"), _auto("Ghost", "GHOST")])
    db_session.flush()

    # fetch_last_close always returns only AAPL; GHOST retry also finds nothing.
    points = {"AAPL": (310.0, _AS_OF)}
    with (
        patch.object(price_fetcher, "fetch_last_close", return_value=points),
        patch("app.services.price_fetcher.time.sleep"),  # skip retry delay
    ):
        result = price_fetcher.update_holding_prices(db_session)

    assert result.updated == 1
    assert result.failed == ["GHOST"]


def test_total_failure_after_retry(db_session: Session) -> None:
    db_session.add(_auto("Apple", "AAPL"))
    db_session.flush()

    with (
        patch.object(price_fetcher, "fetch_last_close", return_value={}),
        patch("app.services.price_fetcher.time.sleep") as mock_sleep,  # don't actually wait
    ):
        result = price_fetcher.update_holding_prices(db_session)

    assert result.updated == 0
    assert result.failed == ["AAPL"]
    mock_sleep.assert_called_once()


def test_partial_failure_retry_succeeds(db_session: Session) -> None:
    """A ticker absent from the first bulk call is retried individually and succeeds."""
    db_session.add_all([_auto("Apple", "AAPL"), _auto("HK Co", "0700.HK")])
    db_session.flush()

    # First call (bulk): only AAPL comes back.
    # Second call (single-ticker retry for 0700.HK): returns the HK price.
    hk_point = {"0700.HK": (350.0, _AS_OF)}
    side_effects = [
        {"AAPL": (310.0, _AS_OF)},  # bulk pass
        hk_point,  # per-ticker retry
    ]
    with (
        patch.object(price_fetcher, "fetch_last_close", side_effect=side_effects),
        patch("app.services.price_fetcher.time.sleep"),
    ):
        result = price_fetcher.update_holding_prices(db_session)

    assert result.updated == 2
    assert result.failed == []

    rows = {h.ticker: h for h in db_session.query(Holding).all()}
    assert rows["0700.HK"].market_price == Decimal("350.0")


def test_partial_failure_retry_also_fails(db_session: Session) -> None:
    """A ticker that fails both the bulk pass and the per-ticker retry lands in failed."""
    db_session.add_all([_auto("Apple", "AAPL"), _auto("HK Co", "0700.HK")])
    db_session.flush()

    side_effects = [
        {"AAPL": (310.0, _AS_OF)},  # bulk pass — 0700.HK absent
        {},  # per-ticker retry for 0700.HK — still nothing
    ]
    with (
        patch.object(price_fetcher, "fetch_last_close", side_effect=side_effects),
        patch("app.services.price_fetcher.time.sleep"),
    ):
        result = price_fetcher.update_holding_prices(db_session)

    assert result.updated == 1
    assert result.failed == ["0700.HK"]


def test_backfill_sectors_only_fills_missing(db_session: Session) -> None:
    have = _auto("Apple", "AAPL")
    have.sector = "Technology"  # already set → skipped
    need = _auto("Msft", "MSFT")  # sector None → filled
    fund = _auto("Fund", "510300.SS", asset_type="fund")  # non-equity → skipped
    db_session.add_all([have, need, fund])
    db_session.flush()

    with patch.object(price_fetcher, "_fetch_yf_sector", return_value="Technology"):
        updated = price_fetcher.backfill_sectors(db_session)

    assert updated == 1
    rows = {h.ticker: h for h in db_session.query(Holding).all()}
    assert rows["MSFT"].sector == "Technology"
    assert rows["510300.SS"].sector is None


def test_backfill_unknown_sector_becomes_other(db_session: Session) -> None:
    db_session.add(_auto("HK Co", "0700.HK"))
    db_session.flush()

    with patch.object(price_fetcher, "_fetch_yf_sector", return_value=None):
        price_fetcher.backfill_sectors(db_session)

    row = db_session.query(Holding).filter_by(ticker="0700.HK").one()
    assert row.sector == "Other"
