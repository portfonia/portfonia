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

    rows = {h.fund_code: h for h in db_session.query(Holding).all()}
    row = rows["005827"]
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


def _patched_client_by_source(
    fundgz_by_code: dict[str, str], sina_by_code: dict[str, str] | None = None
) -> MagicMock:
    """Mock httpx.Client dispatching on URL host (fundgz vs Sina) + fund_code.

    Unlike _patched_client, this distinguishes the two endpoints so a test can
    make fundgz fail and Sina succeed (or vice versa) for the same fund_code.
    fundgz bodies are set as `.text` (JSONP is plain-text/HTML); Sina bodies
    are GBK-encoded and set as `.content` (real Sina responses are GBK, not
    UTF-8 — `_sina_fund_nav` must decode explicitly, not rely on `.text`).
    """

    def _get(url: str, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "sinajs.cn" in url:
            body = next(b for code, b in (sina_by_code or {}).items() if code in url)
            resp.content = body.encode("gbk")
        else:
            body = next(b for code, b in fundgz_by_code.items() if code in url)
            resp.text = body
        return resp

    client = MagicMock()
    client.get.side_effect = _get
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return cm


_EASTMONEY_BLOCK_PAGE = (
    "<!doctype html><html><head><title>页面未找到 - 东方财富网</title></head></html>"
)


def test_sina_fallback_used_when_fundgz_blocked(db_session: Session) -> None:
    """fundgz's Eastmoney app-layer block (HTTP 200, HTML 'page not found' body,
    not JSONP) must fall through to Sina instead of being treated as a terminal
    parse failure — confirmed against real OCI production traffic, issue #20:
    fundgz returns this exact block page for every fund code from production,
    while Sina (hq.sinajs.cn) is reachable and correct."""
    db_session.add(_fund("Blocked on fundgz", "019547"))
    db_session.flush()

    sina_ok = (
        'var hq_str_f_019547="天弘纳斯达克100指数发起式C,1.5882,1.5882,3.9150,2026-08-12,2.05";'
    )

    with patch(
        "app.services.fund_nav_fetcher.httpx.Client",
        return_value=_patched_client_by_source(
            fundgz_by_code={"019547": _EASTMONEY_BLOCK_PAGE},
            sina_by_code={"019547": sina_ok},
        ),
    ):
        result = fund_nav_fetcher.update_fund_navs(db_session)

    assert result.updated == 1
    assert result.failed == []

    # fund_code is Fernet-encrypted at rest (issue #31) — SQL-level equality
    # filters miss stored rows, so fetch-then-filter in Python (same pattern as
    # test_official_nav_parsed_and_anchored_to_cst above).
    rows = {h.fund_code: h for h in db_session.query(Holding).all()}
    row = rows["019547"]
    assert row.market_price == Decimal("1.5882")
    assert row.price_as_of is not None
    as_of_cst = row.price_as_of.astimezone(CST)
    assert as_of_cst.date() == date(2026, 8, 12)
    assert as_of_cst.hour == 15  # A-share close, same anchor as the fundgz path


def test_both_fundgz_and_sina_fail_marks_failed(db_session: Session) -> None:
    """Neither source has data (e.g. a delisted/invalid code) -> failed, no crash."""
    db_session.add(_fund("Unknown everywhere", "999999"))
    db_session.flush()

    with patch(
        "app.services.fund_nav_fetcher.httpx.Client",
        return_value=_patched_client_by_source(
            fundgz_by_code={"999999": _EASTMONEY_BLOCK_PAGE},
            sina_by_code={"999999": 'var hq_str_f_999999="";'},
        ),
    ):
        result = fund_nav_fetcher.update_fund_navs(db_session)

    assert result.updated == 0
    assert result.failed == ["999999"]
