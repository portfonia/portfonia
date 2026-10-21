"""Shared Exa news search with response accounting and publication dates (#812)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal

import httpx
from httpx import post

from app.core.config import Settings
from app.core.timezones import ET
from app.services.instrument_news_sources import mapping, rows, text_value
from app.services.intel_http import quiet_transport
from app.services.paid_search import status_class

OTHER_THEME = "other"


@dataclass(frozen=True)
class ExaHit:
    url: str
    title: str
    published_at: datetime | None
    text: str


@dataclass
class ExaResult:
    http_status: int | None
    status_class: str = "success"
    cost_usd: Decimal = Decimal(0)
    sent_timeout: bool = False
    detail: str = ""
    results: list[ExaHit] = field(default_factory=list)


def published(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return (
                datetime.combine(parsed.date(), time.min, tzinfo=ET)
                if parsed.time() == time.min
                else None
            )
        utc = parsed.astimezone(UTC)
        if utc.time() == time.min:
            return datetime.combine(utc.date(), time.min, tzinfo=ET)
        return parsed
    except ValueError:
        return None


def search(
    settings: Settings,
    query: str,
    domains: list[str],
    start: datetime,
    num_results: int,
    max_characters: int,
) -> ExaResult:
    key = settings.EXA_API_KEY
    if key is None:
        return ExaResult(None, "invalid_key")
    payload = {
        "query": query,
        "type": "auto",
        "category": "news",
        "numResults": num_results,
        "includeDomains": domains,
        "startPublishedDate": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "contents": {"text": {"maxCharacters": max_characters}},
    }
    try:
        with quiet_transport():
            response = post(
                "https://api.exa.ai/search",
                headers={"x-api-key": key.get_secret_value()},
                json=payload,
                timeout=60,
            )
    except httpx.ReadTimeout:
        return ExaResult(None, "error", sent_timeout=True)
    except httpx.HTTPError:
        return ExaResult(None, "error")
    result = ExaResult(response.status_code, status_class(response.status_code))
    try:
        data = mapping(response.json())
        result.cost_usd = Decimal(str(mapping(data.get("costDollars", {})).get("total", 0)))
        if result.status_class == "success":
            result.results = [
                ExaHit(
                    text_value(row.get("url")),
                    text_value(row.get("title")),
                    published(row.get("publishedDate")),
                    text_value(row.get("text")),
                )
                for row in rows(data.get("results", []))
            ]
    except (ValueError, TypeError, ArithmeticError):
        result.status_class = "error"
        result.results = []
    if result.status_class != "success":
        result.detail = " ".join(response.text.split())[:200]
    return result
