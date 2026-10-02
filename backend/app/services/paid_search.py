"""REST adapters with normalized results and explicit response accounting."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from math import ceil

import httpx
from httpx import post

from app.core.config import Settings
from app.core.timezones import ET
from app.services.instrument_news_sources import mapping, rows, text_value
from app.services.intel_http import quiet_transport
from app.services.intel_leads import Lead


@dataclass
class PaidResult:
    http_status: int | None
    units: Decimal
    cost_usd: Decimal
    status_class: str = "success"
    sent_timeout: bool = False
    leads: list[Lead] = field(default_factory=list)
    bodies: dict[str, str] = field(default_factory=dict)

    def budget_amount(self, provider: str) -> Decimal:
        return self.units if provider == "tavily" else self.cost_usd


def status_class(status: int) -> str:
    if 200 <= status < 300:
        return "success"
    if status in (401, 403):
        return "invalid_key"
    if status in (402, 429, 432, 433):
        return "quota_or_rate"
    return "error"


def published(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        return parsed.replace(tzinfo=ET) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


class PaidClient:
    provider: str

    def __init__(self, settings: Settings, chunks: int) -> None:
        self.settings = settings
        self.chunks = chunks

    def _call(self, operation: str, payload: dict[str, object], count: int) -> PaidResult:
        units = Decimal(
            1 if operation == "search" else ceil(count / 5) if self.provider == "tavily" else count
        )
        cost = (
            units * Decimal(".008")
            if self.provider == "tavily"
            else Decimal(".005")
            if operation == "search"
            else units * Decimal(".001")
        )
        key = (
            self.settings.TAVILY_API_KEY
            if self.provider == "tavily"
            else self.settings.PARALLEL_API_KEY
        )
        if not key:
            return PaidResult(None, Decimal(0), Decimal(0), "invalid_key")
        headers = (
            {"Authorization": "Bearer " + key.get_secret_value()}
            if self.provider == "tavily"
            else {"x-api-key": key.get_secret_value()}
        )
        base = (
            "https://api.tavily.com" if self.provider == "tavily" else "https://api.parallel.ai/v1"
        )
        try:
            with quiet_transport():
                response = post(base + "/" + operation, headers=headers, json=payload, timeout=60)
        except httpx.ReadTimeout:
            return PaidResult(None, units, cost, "error", sent_timeout=True)
        except httpx.HTTPError:
            return PaidResult(None, Decimal(0), Decimal(0), "error")
        status = response.status_code
        category = status_class(status)
        if category != "success":
            return PaidResult(status, Decimal(0), Decimal(0), category)
        result = PaidResult(status, units, cost)
        try:
            data = mapping(response.json())
            usage = data.get("usage")
            if (
                self.provider == "tavily"
                and isinstance(usage, dict)
                and isinstance(usage.get("credits"), (int, float))
            ):
                result.units = max(units, Decimal(str(usage["credits"])))
                result.cost_usd = result.units * Decimal(".008")
            for row in rows(data.get("results", [])):
                url = text_value(row.get("url"))
                if not url:
                    continue
                if operation == "search":
                    result.leads.append(
                        Lead(
                            url,
                            text_value(row.get("title")),
                            published(row.get("published_date") or row.get("publish_date")),
                        )
                    )
                else:
                    chunks = row.get("chunks")
                    excerpts = row.get("excerpts")
                    if self.provider == "tavily":
                        body = (
                            "\n".join(text_value(c.get("content")) for c in rows(chunks))
                            if isinstance(chunks, list) and chunks
                            else text_value(row.get("raw_content"))
                        )
                    else:
                        body = text_value(row.get("full_content"))
                        if not body and isinstance(excerpts, list):
                            body = "\n".join(text_value(value) for value in excerpts)
                    result.bodies[url] = body
        except (ValueError, KeyError, TypeError):
            result.status_class = "error"
        return result

    def search(self, query: str, start: date, end: date) -> PaidResult:
        payload: dict[str, object] = (
            {
                "query": query,
                "search_depth": "basic",
                "topic": "news",
                "max_results": 3,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "include_usage": True,
            }
            if self.provider == "tavily"
            else {"objective": query, "search_queries": [query]}
        )
        result = self._call("search", payload, 1)
        result.leads = result.leads[:3]
        return result

    def extract(self, urls: list[str], query: str) -> PaidResult:
        payload: dict[str, object] = (
            {"urls": urls, "query": query, "chunks_per_source": self.chunks, "include_usage": True}
            if self.provider == "tavily"
            else {"urls": urls, "objective": query}
        )
        return self._call("extract", payload, len(urls))


class TavilyClient(PaidClient):
    provider = "tavily"


class ParallelClient(PaidClient):
    provider = "parallel"
