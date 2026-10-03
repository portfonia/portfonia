"""Code cleaning rules and the one-shot public headline classifier."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from app.core.config import OR_ATTRIBUTION_HEADERS, get_settings
from app.services.instrument_news_sources import CollectedItem, mapping, rows
from app.services.instrument_profiles import match_instruments
from app.services.intel_http import quiet_transport


@dataclass(frozen=True)
class CleaningConfig:
    segments: list[str]
    patterns: list[re.Pattern[str]]
    threshold: float
    hours: int


def load_cleaning_config(path: Path | None = None) -> CleaningConfig:
    data = yaml.safe_load(
        (path or Path(__file__).resolve().parents[2] / "config/intel_cleaning.yml").read_text()
    )
    try:
        return CleaningConfig(
            list(data.get("non_article_path_segments", [])),
            [re.compile(p, re.IGNORECASE) for p in data.get("low_value_title_patterns", [])],
            float(data.get("near_duplicate_jaccard", 0.8)),
            int(data.get("near_duplicate_hours", 48)),
        )
    except (re.error, TypeError, ValueError) as exc:
        raise ValueError("invalid cleaning configuration") from exc


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


SYSTEM_PROMPT = 'You classify financial news headlines for one company each. For every item return one label:\nkeep = the item reports a concrete development about THIS company (earnings, deals, products, guidance, legal/regulatory, management, analyst actions, notable price moves with a stated cause);\nmention = the company is only mentioned in passing or is one of many in a broad market piece;\npromo = stock-pick, buy/sell, comparison, prediction or listicle content; institutional holding-change notices (a fund bought, sold or changed its stake); routine price-move recaps with no stated company-specific cause;\nunrelated = not about this company.\nOutput ONLY JSON: {"labels": [{"id": int, "label": "keep|mention|promo|unrelated"}]}'


def classify_headlines(
    items: list[CollectedItem],
    ticker: str,
    aliases: list[str],
    recent_titles: Sequence[str] | None = None,
) -> tuple[dict[int, str], float, str | None]:
    settings = get_settings()
    content = "\n".join(
        f"{i}\t{ticker} ({', '.join(aliases)})\t{x.title}\t{(x.summary or '')[:160]}"
        for i, x in enumerate(items)
    )
    prompt = SYSTEM_PROMPT
    if recent_titles is not None:
        prompt = prompt.replace(
            '"label": "keep|mention|promo|unrelated"}',
            '"label": "keep|mention|promo|unrelated", "duplicate_of": "e<k>" | int | null}',
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
        usage = mapping(data.get("usage") or {})
        cost = usage.get("cost", 0)
        return labels, float(cost) if isinstance(cost, (int, float)) else 0.0, None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        error = f"classifier: {type(exc).__name__}" + (
            f" HTTP {status}" if status is not None else ""
        )
        return {}, 0.0, error
