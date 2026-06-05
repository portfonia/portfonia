"""Fetch public mutual fund NAV from 天天基金 and persist to holdings.

We deliberately read the official settled NAV (`dwjz`, the prior trading
day's closing NAV) rather than the intraday estimate (`gsz`). Settled NAV is
the value you would actually redeem at; the estimate carries error. price_as_of
is therefore the NAV date, not the time we fetched it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import CST
from app.models.holding import Holding
from app.services.price_fetcher import PriceFetchResult

logger = logging.getLogger(__name__)

# 天天基金 realtime endpoint (JSONP); we extract the official NAV field from it.
_NAV_URL = "https://fundgz.1234567.com.cn/js/{fund_code}.js"
_JSONP_RE = re.compile(r"jsonpgz\((\{.*\})\);?", re.DOTALL)

# Mutual fund NAV is struck at A-share close (15:00 CST); anchor price_as_of there.
_AMARKET_CLOSE_HOUR = 15


def _fetch_nav(fund_code: str, client: httpx.Client) -> tuple[Decimal, datetime] | None:
    """
    Fetch NAV for a single fund code.

    Returns (nav, price_as_of) where price_as_of is the NAV date at
    15:00 CST (A-share close). Returns None on any error.
    """
    url = _NAV_URL.format(fund_code=fund_code)
    try:
        resp = client.get(url, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("HTTP error fetching NAV for fund %s", fund_code)
        return None

    match = _JSONP_RE.search(resp.text)
    if not match:
        logger.error("unexpected response format for fund %s: %s", fund_code, resp.text[:200])
        return None

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.exception("JSON parse error for fund %s", fund_code)
        return None

    dwjz = data.get("dwjz")
    jzrq = data.get("jzrq")

    if not dwjz or not jzrq:
        logger.error("missing dwjz or jzrq for fund %s: %s", fund_code, data)
        return None

    try:
        nav = Decimal(str(dwjz))
    except Exception:
        logger.exception("cannot parse dwjz=%r for fund %s", dwjz, fund_code)
        return None

    try:
        nav_date = datetime.strptime(jzrq, "%Y-%m-%d")
        price_as_of = nav_date.replace(hour=_AMARKET_CLOSE_HOUR, minute=0, second=0, tzinfo=CST)
    except ValueError:
        logger.exception("cannot parse jzrq=%r for fund %s", jzrq, fund_code)
        return None

    return nav, price_as_of


def update_fund_navs(session: Session) -> PriceFetchResult:
    """
    Load all auto-mode holdings with a fund_code, fetch NAVs from
    天天基金, and write market_price / price_as_of / price_fetched_at.
    """
    result = PriceFetchResult()

    rows: list[Holding] = list(
        session.execute(
            select(Holding).where(
                Holding.pricing_mode == "auto",
                Holding.fund_code.isnot(None),
            )
        ).scalars()
    )

    if not rows:
        return result

    fetched_at = datetime.now(tz=UTC)

    with httpx.Client() as client:
        for row in rows:
            fund_code = row.fund_code
            assert fund_code is not None  # filtered in query above
            nav_result = _fetch_nav(fund_code, client)
            if nav_result is None:
                result.failed.append(fund_code)
                continue
            nav, price_as_of = nav_result
            row.market_price = nav
            row.price_as_of = price_as_of
            row.price_fetched_at = fetched_at
            result.updated += 1
            logger.info("fund %s NAV=%.4f as_of=%s", fund_code, nav, price_as_of.date())

    session.flush()
    return result
