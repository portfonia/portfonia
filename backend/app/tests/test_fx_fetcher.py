"""Integration tests for fx_fetcher — real Postgres, mocked yfinance."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.services import fx_fetcher

# 16:00 ET on 2026-06-04 → rate_date 2026-06-04 (well clear of midnight).
_AS_OF = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)


def _fake_points() -> dict[str, tuple[float, datetime]]:
    return {yf_ticker: (7.18, _AS_OF) for yf_ticker in fx_fetcher._PAIRS.values()}


def test_upsert_writes_all_pairs(db_session: Session) -> None:
    with patch.object(fx_fetcher, "fetch_last_close", return_value=_fake_points()):
        result = fx_fetcher.update_fx_rates(db_session)

    assert result.upserted == len(fx_fetcher._PAIRS)
    assert result.failed == []

    rows = {r.pair: r for r in db_session.execute(select(FxRate)).scalars()}
    assert set(rows) == set(fx_fetcher._PAIRS)
    assert rows["USDCNY"].rate == Decimal(str(7.18))
    assert rows["USDCNY"].rate_date == date(2026, 6, 4)


def test_upsert_is_idempotent(db_session: Session) -> None:
    with patch.object(fx_fetcher, "fetch_last_close", return_value=_fake_points()):
        fx_fetcher.update_fx_rates(db_session)
        fx_fetcher.update_fx_rates(db_session)

    count = len(list(db_session.execute(select(FxRate)).scalars()))
    assert count == len(fx_fetcher._PAIRS)  # second run updates in place, no duplicates


def test_no_data_marks_all_failed(db_session: Session) -> None:
    with patch.object(fx_fetcher, "fetch_last_close", return_value={}):
        result = fx_fetcher.update_fx_rates(db_session)

    assert result.upserted == 0
    assert set(result.failed) == set(fx_fetcher._PAIRS)


def test_partial_data_records_missing_pair(db_session: Session) -> None:
    points = {"USDCNY=X": (7.18, _AS_OF)}
    with patch.object(fx_fetcher, "fetch_last_close", return_value=points):
        result = fx_fetcher.update_fx_rates(db_session)

    assert result.upserted == 1
    assert set(result.failed) == set(fx_fetcher._PAIRS) - {"USDCNY"}


def test_pairs_cover_every_valid_currency_except_usd() -> None:
    """issue #204: GBP (and 10 other VALID_CURRENCIES entries) had no FX pair,
    so holdings in those currencies silently dropped out of every report
    total regardless of price correctness. Pin the full set so a future
    currency addition to VALID_CURRENCIES can't reintroduce the same gap."""
    from app.schemas.holdings import VALID_CURRENCIES

    assert {pair[3:] for pair in fx_fetcher._PAIRS} == VALID_CURRENCIES - {"USD"}
