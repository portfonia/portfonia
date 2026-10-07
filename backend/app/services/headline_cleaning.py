"""Code cleaning rules and the one-shot public headline classifier."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

import httpx
import yaml
import yfinance as yf

from app.core.config import OR_ATTRIBUTION_HEADERS, get_settings
from app.core.timezones import ET, today_et
from app.services.instrument_news_sources import CollectedItem, mapping, rows
from app.services.instrument_profiles import match_instruments
from app.services.instrument_relations import Relation
from app.services.intel_http import quiet_transport


@dataclass(frozen=True)
class CleaningConfig:
    segments: list[str]
    patterns: list[re.Pattern[str]]
    threshold: float
    hours: int
    earnings_patterns: list[re.Pattern[str]]
    stale_earnings_days: int
    preview_patterns: list[re.Pattern[str]]
    preview_max_days: int
    related_per_instrument: int = 3


def load_cleaning_config(path: Path | None = None) -> CleaningConfig:
    data = yaml.safe_load(
        (path or Path(__file__).resolve().parents[2] / "config/intel_cleaning.yml").read_text()
    )
    stale = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "config/intel_deepen.yml").read_text()
    ).get("headline_cleaning", {})
    try:
        return CleaningConfig(
            list(data.get("non_article_path_segments", [])),
            [re.compile(p, re.IGNORECASE) for p in data.get("low_value_title_patterns", [])],
            float(data.get("near_duplicate_jaccard", 0.8)),
            int(data.get("near_duplicate_hours", 48)),
            [re.compile(p, re.IGNORECASE) for p in stale.get("earnings_recap_patterns", [])],
            int(stale.get("stale_earnings_days", 14)),
            [re.compile(p, re.IGNORECASE) for p in stale.get("earnings_preview_patterns", [])],
            int(stale.get("preview_max_days_ahead", 21)),
            int(data.get("related_per_instrument", 3)),
        )
    except (re.error, TypeError, ValueError) as exc:
        raise ValueError("invalid cleaning configuration") from exc


class EarningsCache:
    """One earnings-date lookup per symbol across collection and paid workers in a slot."""

    def __init__(self) -> None:
        self._cached_dates: dict[str, list[date]] = {}
        self._lock = threading.Lock()

    def _dates(self, symbol: str) -> list[date]:
        with self._lock:
            if symbol not in self._cached_dates:
                try:
                    frame = yf.Ticker(symbol).get_earnings_dates()
                    dates = (
                        []
                        if frame is None
                        else [
                            stamp.astimezone(ET).date()
                            for stamp in frame.index
                            if isinstance(stamp, datetime)
                        ]
                    )
                except Exception:
                    dates = []
                self._cached_dates[symbol] = dates
            return self._cached_dates[symbol]

    def stale_reason(
        self, item: CollectedItem, symbol: str, config: CleaningConfig, *, recap: bool = False
    ) -> str | None:
        """Check previews against the next date, and recaps against the latest past date."""
        if item.kind == "filing":
            return None
        published = item.published_at.astimezone(ET).date()
        if any(p.search(item.title) for p in config.preview_patterns):
            future = [day for day in self._dates(symbol) if day >= published]
            if not future:
                return "stale_lookup_failed"
            return (
                "stale_rule" if (min(future) - published).days > config.preview_max_days else None
            )
        if not recap and not any(p.search(item.title) for p in config.earnings_patterns):
            return None
        past = [day for day in self._dates(symbol) if day <= published]
        if not past:
            return "stale_lookup_failed"
        return "stale_rule" if (published - max(past)).days > config.stale_earnings_days else None


_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "has",
        "have",
        "its",
        "it",
        "as",
        "from",
    ]
)


def tokens(title: str) -> set[str]:
    if re.search(r"[\u3400-\u9fff]", title):
        return {title[i : i + 2].lower() for i in range(len(title) - 1)}
    return set(re.findall(r"\b[a-z0-9]{2,}\b", title.lower())) - _STOPWORDS


def block_reason(
    item: CollectedItem,
    aliases: list[str],
    previous: list[str],
    config: CleaningConfig,
    *,
    pool: bool = False,
    earlier: Sequence[str] = (),
) -> str | None:
    if any(s in urlparse(item.url).path for s in config.segments):
        return "non_article"
    if (
        not pool
        and item.kind != "filing"
        and not match_instruments(item.title + " " + (item.summary or ""), {"instrument": aliases})
    ):
        return "unrelated_rule"
    if any(p.search(item.title) for p in config.patterns):
        return "low_value_rule"
    if not pool:
        current = tokens(item.title)
        for titles, reason in ((earlier, "duplicate_earlier"), (previous, "duplicate")):
            for title in titles:
                other = tokens(title)
                union = current | other
                if union and len(current & other) / len(union) >= config.threshold:
                    return reason
    return None


SYSTEM_PROMPT = 'You classify financial news headlines for one company each. For every item return one label:\nkeep = the item reports a concrete development about THIS company (earnings, deals, products, guidance, legal/regulatory, management, analyst actions, notable price moves with a stated cause);\nmention = the company is only mentioned in passing or is one of many in a broad market piece;\npromo = stock-pick, buy/sell, comparison, prediction or listicle content; institutional holding-change notices (a fund bought, sold or changed its stake); routine price-move recaps with no stated company-specific cause;\nunrelated = not about this company.\nAlso return recap for every item, judged ONLY from the words of the title and summary; never use your own knowledge of when anything happened.\nrecap = true if the item reports or reacts to THIS company\'s own periodic financial results (quarterly or annual revenue, EPS, profit, margins, or guidance issued with results); false otherwise, including operating data such as deliveries, production or sales volumes, and previews of results not yet released.\nOutput ONLY JSON: {"labels": [{"id": int, "label": "keep|mention|promo|unrelated", "recap": true|false}]}'


def classify_headlines(
    items: list[CollectedItem],
    ticker: str,
    aliases: list[str],
    recent_titles: Sequence[str] | None = None,
    *,
    batch_date: date | None = None,
    recap_is_stale: Callable[[CollectedItem], bool] | None = None,
) -> tuple[dict[int, str], float, str | None]:
    settings = get_settings()
    content = "\n".join(
        f"{i}\t{ticker} ({', '.join(aliases)})\t{x.title}\t{(x.summary or '')[:160]}"
        for i, x in enumerate(items)
    )
    prompt = SYSTEM_PROMPT + f"\nBatch date (ET): {batch_date or today_et()}"
    if recent_titles is not None:
        prompt = prompt.replace(
            '"recap": true|false}',
            '"recap": true|false, "duplicate_of": "e<k>" | int | null}',
        )
        prompt += '\nSome items may repeat an event already covered. EXISTING lists earlier headlines for this company. For each item, set "duplicate_of" to the id of an EXISTING headline ("e0", "e1", ...) or of a lower-numbered item in this batch that reports the same event with no new material fact (no new figure, party, or stage). Otherwise set it to null. A follow-up with new facts is not a duplicate.'
        content = (
            "EXISTING:\n"
            + "".join(f"e{i}\t{title}\n" for i, title in enumerate(recent_titles))
            + content
        )
    payload = {
        "model": settings.INTEL_CLASSIFIER_MODEL,
        "reasoning": {"effort": "low"},
        "response_format": {"type": "json_object"},
        "max_tokens": 8000,
        "provider": {"data_collection": "deny"},
        "usage": {"include": True},
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
    }
    try:
        with quiet_transport():
            resp = httpx.post(
                settings.OPENROUTER_BASE_URL.rstrip("/") + "/chat/completions",
                headers={
                    **OR_ATTRIBUTION_HEADERS,
                    "Authorization": "Bearer " + settings.OPENROUTER_API_KEY.get_secret_value(),
                },
                json=payload,
                timeout=60,
            )
        resp.raise_for_status()
        data = mapping(resp.json())
        choices = rows(data["choices"])
        message = mapping(choices[0]["message"])
        raw = message["content"]
        if not isinstance(raw, str):
            raise ValueError("invalid classifier content")
        parsed = mapping(json.loads(raw))
        labels = {}
        for row in rows(parsed["labels"]):
            idx, label = row.get("id"), row.get("label")
            if not isinstance(idx, int) or not 0 <= idx < len(items):
                continue
            duplicate = row.get("duplicate_of")
            if duplicate == f"e{idx}":
                duplicate = None  # the model echoing the item's own number (#653)
            if recent_titles is not None and (
                (type(duplicate) is int and 0 <= duplicate < idx)
                or (
                    isinstance(duplicate, str)
                    and re.fullmatch(r"e[0-9]+", duplicate)
                    and int(duplicate[1:]) < len(recent_titles)
                )
            ):
                labels[idx] = "duplicate"
            elif label in ("keep", "mention", "promo", "unrelated"):
                labels[idx] = str(label)
                if (
                    label in ("keep", "mention")
                    and row.get("recap") is True
                    and recap_is_stale is not None
                    and recap_is_stale(items[idx])
                ):
                    labels[idx] = "stale"
        usage = mapping(data.get("usage") or {})
        cost = usage.get("cost", 0)
        return labels, float(cost) if isinstance(cost, (int, float)) else 0.0, None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        error = f"classifier: {type(exc).__name__}" + (
            f" HTTP {status}" if status is not None else ""
        )
        return {}, 0.0, error


RELATED_PROMPT = 'You review headlines that name a business partner or rival of one company but not the company itself. For each item return keep or drop.\nkeep = the item reports a concrete development about the named related entity (results, guidance, capacity, pricing, orders, supply, a deal, a product launch, legal or regulatory action, an outage) that plausibly affects THIS company through the stated relation;\ndrop = stock picks, buy/sell or valuation commentary, listicles, routine price moves, broad market pieces, opinion or thesis pieces about a stock (for example \'X: The ... Catalyst\', \'X Has the Biggest Upside\'), return projections, and headlines whose only fact is the related entity\'s share-price performance, or anything without a clear link through the stated relation.\nJudge only from the words given.\nOutput ONLY JSON: {"labels": [{"id": int, "label": "keep|drop", "entity": "<name of the related entity it concerns>"}]}'


def classify_related(
    items: list[CollectedItem],
    ticker: str,
    aliases: list[str],
    hits: list[list[Relation]],
) -> tuple[dict[int, dict[str, str]], float, str | None]:
    """One request per batch of related-entity headlines (#681); no retry."""
    content = "\n".join(
        f"{i}\t{ticker} ({', '.join(aliases)})\trelated: "
        + "; ".join(f"{r.name} ({r.relation})" for r in hits[i])
        + f"\t{x.title}\t{(x.summary or '')[:160]}"
        for i, x in enumerate(items)
    )
    try:
        data, cost = openrouter_json(RELATED_PROMPT, content)
        labels: dict[int, dict[str, str]] = {}
        for row in rows(data["labels"]):
            idx, label = row.get("id"), row.get("label")
            if isinstance(idx, int) and 0 <= idx < len(items) and label in ("keep", "drop"):
                labels[idx] = {"label": str(label), "entity": str(row.get("entity") or "")}
        return labels, cost, None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        return {}, 0.0, classifier_error(exc)


def classifier_error(exc: Exception) -> str:
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    return f"classifier: {type(exc).__name__}" + (f" HTTP {status}" if status is not None else "")


def openrouter_json(system: str, content: str) -> tuple[dict[str, object], float]:
    """One classifier-model JSON request with `data_collection: deny`; raises on failure."""
    settings = get_settings()
    with quiet_transport():
        resp = httpx.post(
            settings.OPENROUTER_BASE_URL.rstrip("/") + "/chat/completions",
            headers={
                **OR_ATTRIBUTION_HEADERS,
                "Authorization": "Bearer " + settings.OPENROUTER_API_KEY.get_secret_value(),
            },
            json={
                "model": settings.INTEL_CLASSIFIER_MODEL,
                "reasoning": {"effort": "low"},
                "response_format": {"type": "json_object"},
                "max_tokens": 8000,
                "provider": {"data_collection": "deny"},
                "usage": {"include": True},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
            },
            timeout=60,
        )
    resp.raise_for_status()
    data = mapping(resp.json())
    message = mapping(rows(data["choices"])[0]["message"])
    raw = message["content"]
    if not isinstance(raw, str):
        raise ValueError("invalid classifier content")
    usage = mapping(data.get("usage") or {})
    cost = usage.get("cost", 0)
    return mapping(json.loads(raw)), float(cost) if isinstance(cost, (int, float)) else 0.0


class MacroLabel(TypedDict):
    type: str
    importance: int
    event: str


def macro_label(record: dict[str, object]) -> MacroLabel | None:
    """The shared URL-free ranking metadata boundary for a headline (#688/#690)."""
    from app.services.intel_body import without_urls

    kind, importance, event = record.get("type"), record.get("importance"), record.get("event")
    if isinstance(event, str):
        event = without_urls(event).strip()
    if (
        kind not in ("development", "commentary", "off_topic")
        or type(importance) is not int
        or not 1 <= importance <= 3
        or not isinstance(event, str)
        or not event.strip()
    ):
        return None
    return {"type": str(kind), "importance": importance, "event": event}


def near_duplicate_title(left: str, right: str, threshold: float) -> bool:
    """Use the existing headline Jaccard rule for cross-call event identity."""
    a, b = tokens(left), tokens(right)
    union = a | b
    return bool(union) and len(a & b) / len(union) >= threshold


MACRO_PROMPT = """You classify public financial news headlines for one macro theme.
Judge only the supplied title and summary. Return one label per item:
development = a concrete data release, policy decision, official action or market-moving event;
commentary = opinion, an interested party interview, a promotional letter or a firm's outlook without a new macro development;
off_topic = merely contains a macro keyword, with no macro development (including sports or an individual company's routine contract).
importance = systemic significance: 3 for major economy-wide data, central-bank decisions or broad shocks; 2 for other material macro developments; 1 for limited systemic significance.
event = a short lowercase hyphenated slug describing the specific event. Reports of the same event with no new material fact share a slug. Slugs are comparable only within this request.
Do not provide investment advice. Stay within Layer 3: facts, contextual relationships and observable signals, never instructions, price targets or forecasts.
Output ONLY JSON: {"labels": [{"id": int, "type": "development|commentary|off_topic", "importance": 1|2|3, "event": "short-slug"}]}"""


def classify_macro(items: list[CollectedItem]) -> tuple[dict[int, MacroLabel], float, str | None]:
    """One scheduled, classifier-model request per macro unit; no retry."""
    from app.services.intel_body import without_urls

    content = "\n".join(
        f"{i}\t{without_urls(item.title)}\t{without_urls(item.summary or '')[:500]}"
        for i, item in enumerate(items)
    )
    try:
        data, cost = openrouter_json(MACRO_PROMPT, content)
        labels: dict[int, MacroLabel] = {}
        for row in rows(data["labels"]):
            idx = row.get("id")
            label = macro_label(row)
            if type(idx) is int and 0 <= idx < len(items) and label is not None:
                labels[idx] = label
        return labels, cost, None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        return {}, 0.0, classifier_error(exc)
