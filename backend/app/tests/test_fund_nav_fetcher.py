"""Integration tests for fund_nav_fetcher — real Postgres, mocked HTTP."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.core.timezones import CST
from app.models.holding import Holding
from app.services import fund_nav_fetcher

_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")

_JSONP_OK = (
    'jsonpgz({"fundcode":"005827","name":"易方达蓝筹精选混合",'
    '"jzrq":"2026-06-04","dwjz":"1.5800","gsz":"1.5790",'
    '"gszzl":"-0.07","gztime":"2026-06-05 10:45"});'
)


def _fund(name: str, fund_code: str) -> Holding:
    return Holding(
        user_id=_USER,
        name=name,
        pricing_mode="auto",
        fund_code=fund_code,
        currency="CNY",
        shares=Decimal("1000"),
        asset_type="fund",
    )


def _patched_client(body_by_code: dict[str, str]) -> MagicMock:
    """Mock httpx.Client whose .get dispatches on the fund_code in the URL.

    Robust to DB row ordering — each fund_code always gets its own body.
    """

    def _get(url: str, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.text = next(body for code, body in body_by_code.items() if code in url)
        resp.raise_for_status = MagicMock()
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm


def test_fetch_nav_rejects_non_six_digit_code_without_request() -> None:
    """Boundary guard: a malformed fund_code must be skipped before any HTTP call."""
    client = MagicMock()
    for bad in ("../etc", "00582", "0058271", "abcdef", ""):
        assert fund_nav_fetcher._fetch_nav(bad, client) is None
    client.get.assert_not_called()


def test_official_nav_parsed_and_anchored_to_cst(db_session: Session) -> None:
    db_session.add(_fund("E Fund Blue Chip", "005827"))
    db_session.flush()

    with patch(
        "app.services.fund_nav_fetcher.httpx.Client",
        return_value=_patched_client({"005827": _JSONP_OK}),
    ):
        result = fund_nav_fetcher.update_fund_navs(db_session)

    assert result.updated == 1
    assert result.failed == []

    row = db_session.query(Holding).filter_by(fund_code="005827").one()
    assert row.market_price == Decimal("1.5800")  # dwjz, not gsz
    assert row.price_as_of is not None
    # Stored as TIMESTAMPTZ; normalise to CST to check the anchored instant.
    as_of_cst = row.price_as_of.astimezone(CST)
    assert as_of_cst.date() == date(2026, 6, 4)  # jzrq
    assert as_of_cst.hour == 15  # A-share close


def test_malformed_response_marks_failed(db_session: Session) -> None:
    db_session.add(_fund("Bad Fund", "999999"))
    db_session.flush()

    with patch(
        "app.services.fund_nav_fetcher.httpx.Client",
        return_value=_patched_client({"999999": "not jsonp at all"}),
    ):
        result = fund_nav_fetcher.update_fund_navs(db_session)

    assert result.updated == 0
    assert result.failed == ["999999"]


def test_one_bad_one_good_does_not_abort_batch(db_session: Session) -> None:
    db_session.add_all([_fund("Good", "005827"), _fund("Bad", "999999")])
    db_session.flush()

    with patch(
        "app.services.fund_nav_fetcher.httpx.Client",
        return_value=_patched_client({"005827": _JSONP_OK, "999999": "garbage"}),
    ):
        result = fund_nav_fetcher.update_fund_navs(db_session)

    assert result.updated == 1
    assert result.failed == ["999999"]
