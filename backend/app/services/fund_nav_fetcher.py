"""Fetch public mutual fund NAV from Tiantian Fund and persist to holdings / price_snapshots.

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

Realtime path (path 1) has a Sina Finance fallback: fundgz's Eastmoney JSONP
endpoint returns an app-layer block page (HTTP 200, HTML "页面未找到") for every
fund code as of 2026-08-10, confirmed against real OCI production traffic
(issue #20) — matches the same block first documented in the sibling
`portfolio-agent` project's `collector_v2.py` (`_sina_fund_nav`, 2026-07-30),
whose two-attempt-retry/GBK-decode pattern this fallback is ported from. The
historical path (path 2, `fetch_nav_history`/lsjz) has no such block and needs
no fallback — confirmed reachable from the same OCI host.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import CST
from app.models.holding import Holding

logger = logging.getLogger(__name__)


@dataclass
class FundNavFetchResult:
    """Outcome of update_fund_navs(): fund NAV capture, not stock-price capture.

    Same shape as price_fetcher.py's PriceFetchResult by coincidence, not by
    a shared contract — fund NAV (Tiantian Fund) and stock price (yfinance)
    are different domains with different data sources/cadence/failure modes.
    Deliberately not imported from price_fetcher.py (issue #42): a future
    stock-price-specific field added there should not silently affect fund
    NAV capture.
    """

    updated: int = 0
    failed: list[str] = field(default_factory=list)


# Tiantian Fund realtime endpoint (JSONP); extracts the latest official settled NAV.
_NAV_URL = "https://fundgz.1234567.com.cn/js/{fund_code}.js"
_JSONP_RE = re.compile(r"jsonpgz\((\{.*\})\);?", re.DOTALL)

# Sina Finance realtime endpoint — fallback when fundgz returns Eastmoney's
# app-layer block page instead of JSONP. Response format:
#   var hq_str_f_{code}="name,nav,nav_repeated,cumulative_nav,nav_date,...";
# GBK-encoded, not UTF-8 — must decode explicitly (ported from portfolio-agent's
# _sina_fund_nav, which cross-validated this endpoint against Tencent's
# qt.gtimg.cn/q=jj{code} for a second independent source).
_SINA_NAV_URL = "https://hq.sinajs.cn/list=f_{fund_code}"
_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

# Tiantian Fund historical NAV list endpoint.
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


def _anchor_nav_date(date_str: str) -> datetime | None:
    """Parse a YYYY-MM-DD NAV date and anchor it to A-share close (15:00 CST).

    Shared by both NAV sources — fundgz's `jzrq` and Sina's date field use the
    same format. Returns None on any parse error.
    """
    try:
        nav_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    return nav_date.replace(hour=_AMARKET_CLOSE_HOUR, minute=0, second=0, tzinfo=CST)


def _fetch_nav_fundgz(fund_code: str, client: httpx.Client) -> tuple[Decimal, datetime] | None:
    """Fetch NAV from the Tiantian Fund (fundgz) JSONP endpoint. Returns None on any error.

    Logged at WARNING, not ERROR: the Eastmoney block page (see module
    docstring) makes a fundgz miss the expected, Sina-recoverable case as of
    2026-08-10, not a terminal failure — `_fetch_nav` logs ERROR only if
    Sina also fails. Logging this per-fund at ERROR would flood the log with
    one false-alarm line per successfully-refreshed fund (review finding).
    """
    url = _NAV_URL.format(fund_code=fund_code)
    try:
        resp = client.get(url, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("HTTP error fetching fundgz NAV for fund %s", fund_code, exc_info=True)
        return None

    match = _JSONP_RE.search(resp.text)
    if not match:
        logger.warning(
            "unexpected fundgz response format for fund %s: %s", fund_code, resp.text[:200]
        )
        return None

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("JSON parse error for fund %s", fund_code, exc_info=True)
        return None

    dwjz = data.get("dwjz")
    jzrq = data.get("jzrq")

    if not dwjz or not jzrq:
        logger.warning("missing dwjz or jzrq for fund %s: %s", fund_code, data)
        return None

    try:
        nav = Decimal(str(dwjz))
    except Exception:
        logger.warning("cannot parse dwjz=%r for fund %s", dwjz, fund_code, exc_info=True)
        return None

    price_as_of = _anchor_nav_date(jzrq)
    if price_as_of is None:
        logger.warning("cannot parse jzrq=%r for fund %s", jzrq, fund_code)
        return None

    return nav, price_as_of


def _sina_fund_nav(fund_code: str, client: httpx.Client) -> tuple[Decimal, datetime] | None:
    """Fallback NAV source when fundgz is blocked (see module docstring, issue #20).

    Two-attempt retry (increasing timeout) ported from portfolio-agent's
    `_sina_fund_nav` — that project measured 2.2s-8.2s response latency
    variance on this same endpoint, absorbed by retrying rather than treating
    a slow response as unavailable. Retry is scoped to transport/timeout
    failures only (httpx.HTTPError) — a well-formed HTTP 200 that fails to
    parse won't change on a second GET, so that path returns None immediately
    rather than wasting a second round-trip on the synchronous
    /admin/portfolio/refresh request path (review finding). Returns None if
    unrecoverable.
    """
    url = _SINA_NAV_URL.format(fund_code=fund_code)
    # Anchor on the field name, not "any first quoted string" — a block/error
    # page that happens to contain some other quoted comma-string could
    # otherwise be misread as NAV data (review nit).
    line_re = re.compile(rf'hq_str_f_{re.escape(fund_code)}="([^"]+)"')
    for timeout in (10, 15):
        try:
            resp = client.get(url, headers=_SINA_HEADERS, timeout=timeout)
            resp.raise_for_status()
        except httpx.HTTPError:
            logger.warning(
                "HTTP error fetching Sina NAV for fund %s (timeout=%ds)",
                fund_code,
                timeout,
                exc_info=True,
            )
            continue

        try:
            text = resp.content.decode("gbk", errors="replace")
            match = line_re.search(text)
            if not match:
                logger.warning(
                    "unexpected Sina response format for fund %s: %s", fund_code, text[:200]
                )
                return None
            fields = match.group(1).split(",")
            if len(fields) < 5:
                logger.warning("unexpected Sina field count for fund %s: %r", fund_code, fields)
                return None
            nav = Decimal(fields[1])
            if nav <= 0:
                logger.warning("non-positive Sina NAV for fund %s: %r", fund_code, fields[1])
                return None
            price_as_of = _anchor_nav_date(fields[4])
            if price_as_of is None:
                logger.warning("cannot parse Sina nav date=%r for fund %s", fields[4], fund_code)
                return None
            return nav, price_as_of
        except Exception:
            logger.exception("error parsing Sina response for fund %s", fund_code)
            return None

    return None


def _fetch_nav(fund_code: str, client: httpx.Client) -> tuple[Decimal, datetime] | None:
    """
    Fetch NAV for a single fund code — Tiantian Fund (fundgz) first, falling
    back to Sina Finance if that's blocked (see module docstring, issue #20).

    Returns (nav, price_as_of) where price_as_of is the NAV date at
    15:00 CST (A-share close). Returns None if both sources fail.
    """
    # Boundary guard: fund_code is interpolated into the request URL and
    # originates from LLM-parsed holdings. CN mutual-fund codes are exactly six
    # digits; reject anything else so malformed codes can't shape the URL or
    # waste a request.
    if not re.fullmatch(r"\d{6}", fund_code):
        logger.warning("skipping NAV fetch for invalid fund_code %r (expect 6 digits)", fund_code)
        return None

    result = _fetch_nav_fundgz(fund_code, client)
    if result is not None:
        return result

    result = _sina_fund_nav(fund_code, client)
    if result is None:
        # Terminal: both sources failed. fundgz's own miss is logged at
        # WARNING (expected, Sina-recoverable) — this is the one ERROR log
        # that should actually page someone.
        logger.error("fund %s: both fundgz and Sina failed to return a NAV", fund_code)
    return result


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


def update_fund_navs(session: Session) -> FundNavFetchResult:
    """
    Load all auto-mode holdings with a fund_code, fetch NAVs from
    Tiantian Fund, and write market_price / price_as_of / price_fetched_at.
    """
    result = FundNavFetchResult()

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
