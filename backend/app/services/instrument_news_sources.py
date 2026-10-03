"""Free provider adapters. Provider URLs live only in CollectedItem memory."""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlparse

import feedparser
import httpx

from app.core.config import get_settings
from app.core.timezones import ET
from app.services.intel_http import quiet_transport
from app.services.news_fetcher import (
    NewsItem,
    _parse_entry_dt,
    _strip_html,
    strip_google_suffix,
    url_hash,
)


@dataclass
class CollectedItem:
    title: str
    published_at: datetime
    url: str
    summary: str | None = None
    url_kind: str = "direct"
    kind: str = "article"
    filing_form: str | None = None
    identifier: str = ""
    news_id: uuid.UUID | None = None

    def headline(self) -> NewsItem:
        return NewsItem(
            url_hash(self.url), self.title, self.url, "", self.published_at, self.summary
        )


_INTERVALS = {
    "news.google.com": 1.0,
    "finnhub.io": 1.1,
    "www.sec.gov": 0.12,
    "query1.finance.yahoo.com": 0.5,
    "np-anotice-stock.eastmoney.com": 0.5,
}
_LOCKS = {host: threading.Lock() for host in _INTERVALS}
_LAST: dict[str, float] = {}
_CIK: dict[str, str] = {}
_CIK_LOCK = threading.Lock()


def reset_run_cache() -> None:
    with _CIK_LOCK:
        _CIK.clear()


def request(url: str, *, params: dict[str, str | int] | None = None) -> httpx.Response:
    host = urlparse(url).netloc
    headers = {
        "User-Agent": f"Portfonia/0.1 {get_settings().ADMIN_EMAIL}"
        if host == "www.sec.gov"
        else "Mozilla/5.0"
    }
    lock = _LOCKS[host]
    for attempt in range(3):
        try:
            with lock:
                wait = _INTERVALS[host] - (time.monotonic() - _LAST.get(host, float("-inf")))
                if wait > 0:
                    time.sleep(wait)
                _LAST[host] = time.monotonic()
                with quiet_transport(), httpx.Client(timeout=12) as client:
                    resp = client.get(url, params=params, headers=headers, follow_redirects=True)
                resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code != 429
                and exc.response.status_code < 500
            ):
                raise
            if attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("request attempts exhausted")


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid response object")
    return cast(dict[str, object], value)


def rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("invalid response list")
    return [mapping(x) for x in value]


def text_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def timestamp(value: object) -> datetime:
    if not isinstance(value, (int, float)):
        raise ValueError("invalid timestamp")
    return datetime.fromtimestamp(value, UTC)


def fetch_finnhub(ticker: str, since: datetime, until: datetime) -> list[CollectedItem]:
    key = get_settings().FINNHUB_API_KEY
    if key is None:
        raise ValueError("key not set")
    data = request(
        "https://finnhub.io/api/v1/company-news",
        params={
            "symbol": ticker,
            "from": since.date().isoformat(),
            "to": until.date().isoformat(),
            "token": key.get_secret_value(),
        },
    ).json()
    result = []
    for row in rows(data):
        pub = timestamp(row.get("datetime"))
        title = text_value(row.get("headline"))
        url = text_value(row.get("url"))
        if since <= pub <= until and title and url:
            result.append(
                CollectedItem(
                    title,
                    pub,
                    url,
                    text_value(row.get("summary"))[:500] or None,
                    "finnhub_redirect" if urlparse(url).netloc == "finnhub.io" else "direct",
                )
            )
    return result


def fetch_google_news(
    name: str, lang: str, since: datetime, until: datetime
) -> list[CollectedItem]:
    gl, ceid = {
        "en-US": ("US", "US:en"),
        "zh-CN": ("CN", "CN:zh-Hans"),
        "zh-HK": ("HK", "HK:zh-Hant"),
    }[lang]
    query = (
        f'"{name}"'
        + (" stock" if lang == "en-US" else "")
        + f" after:{since.date()} before:{(until + timedelta(days=1)).date()}"
    )
    feed = feedparser.parse(
        request(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": lang, "gl": gl, "ceid": ceid},
        ).content
    )
    result = []
    for entry in feed.entries:
        pub = _parse_entry_dt(entry)
        if pub is not None and since <= pub <= until and entry.get("link"):
            result.append(
                CollectedItem(
                    strip_google_suffix(str(entry.get("title", "")), entry.get("source")),
                    pub,
                    str(entry["link"]),
                    None,
                    "google_news",
                )
            )
    return result


def fetch_yahoo(query: str, symbol: str, since: datetime, until: datetime) -> list[CollectedItem]:
    data = mapping(
        request(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": query, "quotesCount": 0, "newsCount": 20},
        ).json()
    )
    result = []
    for row in rows(data.get("news", [])):
        tickers = row.get("relatedTickers", [])
        if not isinstance(tickers, list) or symbol not in tickers:
            continue
        pub = timestamp(row.get("providerPublishTime"))
        url = text_value(row.get("link"))
        title = text_value(row.get("title"))
        if since <= pub <= until and title and url:
            result.append(CollectedItem(title, pub, url))
    return result


def eastmoney_rows(code: str, ann_type: str) -> list[dict[str, object]]:
    data = mapping(
        request(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            params={
                "sr": -1,
                "page_size": 20,
                "page_index": 1,
                "ann_type": ann_type,
                "client_source": "web",
                "stock_list": code,
            },
        ).json()
    )
    return rows(mapping(data.get("data") or {}).get("list", []))


def fetch_eastmoney_ann(
    code: str, ann_type: str, since: datetime, until: datetime
) -> list[CollectedItem]:
    result = []
    for row in eastmoney_rows(code, ann_type):
        day = datetime.fromisoformat(text_value(row.get("notice_date"))[:10]).replace(tzinfo=ET)
        if since.astimezone(ET).date() <= day.date() <= until.astimezone(ET).date():
            art = text_value(row.get("art_code"))
            title = text_value(row.get("title"))
            if art and title:
                result.append(
                    CollectedItem(
                        title,
                        day,
                        f"https://data.eastmoney.com/notices/detail/{code}/{art}.html",
                        url_kind="filing",
                        kind="filing",
                        filing_form="announcement",
                    )
                )
    return result


def fetch_sec_8k(ticker: str, since: datetime, until: datetime) -> list[CollectedItem]:
    with _CIK_LOCK:
        if not _CIK:
            data = mapping(request("https://www.sec.gov/files/company_tickers.json").json())
            for raw in data.values():
                row = mapping(raw)
                _CIK[text_value(row.get("ticker"))] = str(row.get("cik_str", ""))
        cik = _CIK.get(ticker)
    if not cik:
        return []
    feed = feedparser.parse(
        request(
            "https://www.sec.gov/cgi-bin/browse-edgar",
            params={
                "action": "getcompany",
                "CIK": cik,
                "type": "8-K",
                "count": 40,
                "output": "atom",
                "dateb": (until + timedelta(days=1)).strftime("%Y%m%d"),
            },
        ).content
    )
    result = []
    for entry in feed.entries:
        pub = _parse_entry_dt(entry)
        if (
            pub is None
            or not since <= pub <= until
            or not str(entry.get("title", "")).startswith("8-K")
        ):
            continue
        content = str(entry.get("items-desc", "")) or _strip_html(str(entry.get("summary", "")))
        item_numbers = re.findall(r"(?:item\s*)?(\d+\.\d{2})", content, re.IGNORECASE)
        accession = str(entry.get("accession-number", ""))
        if not accession:
            match = re.search(r"(\d{10}-\d{2}-\d{6})", str(entry.get("id", "")) + " " + content)
            accession = match.group(1) if match else ""
        form = "8-K" + (" Item " + ", ".join(item_numbers) if item_numbers else "")
        result.append(
            CollectedItem(
                form + (" " + accession if accession else ""),
                pub,
                str(entry.get("link", "")),
                url_kind="filing",
                kind="filing",
                filing_form=form,
            )
        )
    return result
