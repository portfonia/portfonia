"""Fetch public mutual fund NAV from 天天基金 and persist to holdings / price_snapshots.

Two data paths:
  1. Realtime (latest settled NAV) — used by update_fund_navs to keep
     holdings.market_price current.  Reads `dwjz` (prior-day settled NAV),
     not `gsz` (intraday estimate), so price_as_of is the NAV date.
  2. Historical (lookback window) — used by capture_fund_navs in price_capture
     to populate price_snapshots so window anomaly detection can cover funds.
     Also uses settled NAV (DWJZ field in the lsjz response).

Note: settled NAV is published after the A-share close (usually same evening),
so the capture may run the next calendar day for the previous trade date — this
is expected and the trade_date column reflects the NAV date, not capture time.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import CST
from app.models.holding import Holding
from app.services.price_fetcher import PriceFetchResult

logger = logging.getLogger(__name__)

# 天天基金 realtime endpoint (JSONP); extracts the latest official settled NAV.
_NAV_URL = "https://fundgz.1234567.com.cn/js/{fund_code}.js"
_JSONP_RE = re.compile(r"jsonpgz\((\{.*\})\);?", re.DOTALL)

# 天天基金 historical NAV list endpoint.
# Returns JSON: {"Data": {"LSJZList": [{"FSRQ": "YYYY-MM-DD", "DWJZ": "1.2345"}, ...]}}
_LSJZ_URL = (
    "http://api.fund.eastmoney.com/f10/lsjz"
    "?fundCode={fund_code}&pageIndex=1&pageSize={page_size}"
    "&startDate={start}&endDate={end}"
)
_LSJZ_HEADERS = {
    "Referer": "https://fund.eastmoney.com/",
    "User-Agent": "Mozilla/5.0",
}

# Mutual fund NAV is struck at A-share close (15:00 CST); anchor there.
_AMARKET_CLOSE_HOUR = 15


def _fetch_nav(fund_code: str, client: httpx.Client) -> tuple[Decimal, datetime] | None:
    """
    Fetch NAV for a single fund code.

    Returns (nav, price_as_of) where price_as_of is the NAV date at
    15:00 CST (A-share close). Returns None on any error.
    """
    # Boundary guard: fund_code is interpolated into the request URL and
    # originates from LLM-parsed holdings. CN mutual-fund codes are exactly six
    # digits; reject anything else so malformed codes can't shape the URL or
    # waste a request.
    if not re.fullmatch(r"\d{6}", fund_code):
        logger.warning("skipping NAV fetch for invalid fund_code %r (expect 6 digits)", fund_code)
        return None
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


def fetch_nav_history(
    fund_code: str, client: httpx.Client, lookback_days: int = 30
) -> list[tuple[date, Decimal]]:
    """Fetch settled NAV history from the lsjz endpoint.

    Returns list of (nav_date, nav) sorted date ascending. Empty on any error.
    """
    if not re.fullmatch(r"\d{6}", fund_code):
        logger.warning("skipping NAV history for invalid fund_code %r", fund_code)
        return []
    end = date.today()
    start = end - timedelta(days=lookback_days)
    url = _LSJZ_URL.format(
        fund_code=fund_code,
        page_size=lookback_days + 5,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
    )
    try:
        resp = client.get(url, headers=_LSJZ_HEADERS, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("HTTP error fetching NAV history for fund %s", fund_code)
        return []

    try:
        payload = resp.json()
    except Exception:
        logger.exception("JSON parse error for fund %s history", fund_code)
        return []

    if payload.get("ErrCode") != 0:
        logger.error("LSJZ API error for fund %s: ErrCode=%s", fund_code, payload.get("ErrCode"))
        return []

    rows = (payload.get("Data") or {}).get("LSJZList") or []
    result: list[tuple[date, Decimal]] = []
    for row in rows:
        fsrq = row.get("FSRQ")
        dwjz = row.get("DWJZ")
        if not fsrq or not dwjz:
            continue
        try:
            nav_date = datetime.strptime(fsrq, "%Y-%m-%d").date()
            nav = Decimal(str(dwjz))
        except Exception:
            logger.warning("skipping unparseable row for fund %s: %r", fund_code, row)
            continue
        result.append((nav_date, nav))

    result.sort(key=lambda t: t[0])
    return result


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
