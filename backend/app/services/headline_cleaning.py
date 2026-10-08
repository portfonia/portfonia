"""Code cleaning rules and the one-shot public headline classifier."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.parse import urlparse

import httpx
import yaml

from app.core.config import OR_ATTRIBUTION_HEADERS, get_settings
from app.services.instrument_news_sources import CollectedItem, mapping, rows
from app.services.instrument_profiles import match_instruments
from app.services.instrument_relations import Relation
from app.services.intel_http import quiet_transport
from app.services.llm_json import parse_llm_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleaningConfig:
    segments: list[str]
    patterns: list[re.Pattern[str]]
    threshold: float
    hours: int
    related_per_instrument: int = 3


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
            int(data.get("related_per_instrument", 3)),
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


CLASSIFIER_ERRORS = (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError)
_DROPPED = ("promo", "unrelated", "duplicate")
STAGE_STATS = ("screen_cost_usd", "review_cost_usd", "retries", "screen_failed", "review_failed")


def _add(stats: dict[str, float] | None, key: str, value: float) -> None:
    if stats is not None:
        stats[key] = stats.get(key, 0) + value


def _stage(
    system: str, content: str, stage: str, stats: dict[str, float] | None
) -> tuple[dict[str, object] | None, float, str | None]:
    """One classifier stage (#700): `screen` or `review`, retried once inside openrouter_json."""
    settings = get_settings()
    model = settings.INTEL_SCREEN_MODEL if stage == "screen" else settings.INTEL_CLASSIFIER_MODEL
    try:
        data, cost = openrouter_json(system, content, model=model, stats=stats, required="labels")
    except CLASSIFIER_ERRORS as exc:
        cost = float(getattr(exc, "cost_usd", 0.0))
        _add(stats, f"{stage}_cost_usd", cost)
        _add(stats, f"{stage}_failed", 1)
        return None, cost, classifier_error(exc).replace("classifier:", f"classifier: {stage}", 1)
    _add(stats, f"{stage}_cost_usd", cost)
    return data, cost, None


def _stage_error(screen: str | None, review: str | None, any_succeeded: bool) -> str | None:
    errors = [e for e in (screen, review) if e]
    if not errors:
        return None
    if any_succeeded:
        return errors[0] + " (fail-open)"
    return "; ".join(errors)


def _headline_content(
    items: list[CollectedItem], ticker: str, aliases: list[str], recent_titles: Sequence[str] | None
) -> str:
    content = "\n".join(
        f"{i}\t{ticker} ({', '.join(aliases)})\t{x.title}\t{(x.summary or '')[:160]}"
        for i, x in enumerate(items)
    )
    if recent_titles is not None:
        content = (
            "EXISTING:\n"
            + "".join(f"e{i}\t{title}\n" for i, title in enumerate(recent_titles))
            + content
        )
    return content


def _headline_labels(
    data: dict[str, object], count: int, recent_titles: Sequence[str] | None
) -> dict[int, str]:
    labels: dict[int, str] = {}
    for row in rows(data["labels"]):
        idx, label = row.get("id"), row.get("label")
        if not isinstance(idx, int) or not 0 <= idx < count:
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
    return labels


def classify_headlines(
    items: list[CollectedItem],
    ticker: str,
    aliases: list[str],
    recent_titles: Sequence[str] | None = None,
    stats: dict[str, float] | None = None,
) -> tuple[dict[int, str], float, str | None]:
    """Screen with INTEL_SCREEN_MODEL, review survivors with INTEL_CLASSIFIER_MODEL (#700).

    A headline is dropped when either stage drops it; `keep` needs both stages.
    A stage that still fails after its retry contributes nothing (fail-open).
    """
    prompt = SYSTEM_PROMPT
    if recent_titles is not None:
        prompt = prompt.replace(
            '"label": "keep|mention|promo|unrelated"}',
            '"label": "keep|mention|promo|unrelated", "duplicate_of": "e<k>" | int | null}',
        )
        prompt += '\nSome items may repeat an event already covered. EXISTING lists earlier headlines for this company. For each item, set "duplicate_of" to the id of an EXISTING headline ("e0", "e1", ...) or of a lower-numbered item in this batch that reports the same event with no new material fact (no new figure, party, or stage). Otherwise set it to null. A follow-up with new facts is not a duplicate.'
    count = len(items)
    data, cost, screen_error = _stage(
        prompt, _headline_content(items, ticker, aliases, recent_titles), "screen", stats
    )
    screen = _headline_labels(data, count, recent_titles) if data is not None else {}
    survivors = [i for i in range(count) if screen.get(i) not in _DROPPED]
    review: dict[int, str] = {}
    review_ok = False
    review_error = None
    if survivors:
        subset = [items[i] for i in survivors]
        data2, cost2, review_error = _stage(
            prompt, _headline_content(subset, ticker, aliases, recent_titles), "review", stats
        )
        cost += cost2
        if data2 is not None:
            review_ok = True
            review = {
                survivors[k]: v
                for k, v in _headline_labels(data2, len(subset), recent_titles).items()
            }
    labels: dict[int, str] = {}
    for i in range(count):
        first, second = screen.get(i), review.get(i)
        if first in _DROPPED:
            labels[i] = str(first)
        elif second in _DROPPED:
            labels[i] = str(second)
        elif first and second:
            labels[i] = "keep" if first == second == "keep" else "mention"
        elif first or second:
            # `keep` needs both stages: one that answered but omitted the item
            # downgrades it; only a whole failed stage is skipped (fail-open).
            other_answered = (review_ok and i in survivors) if first else data is not None
            only = str(first or second)
            labels[i] = "mention" if other_answered and only == "keep" else only
    error = _stage_error(screen_error, review_error, data is not None or review_ok)
    return labels, cost, error


RELATED_PROMPT = 'You review headlines that name a business partner or rival of one company but not the company itself. For each item return keep or drop.\nkeep = the item reports a concrete development about the named related entity (results, guidance, capacity, pricing, orders, supply, a deal, a product launch, legal or regulatory action, an outage) that plausibly affects THIS company through the stated relation;\ndrop = stock picks, buy/sell or valuation commentary, listicles, routine price moves, broad market pieces, opinion or thesis pieces about a stock (for example \'X: The ... Catalyst\', \'X Has the Biggest Upside\'), return projections, and headlines whose only fact is the related entity\'s share-price performance, or anything without a clear link through the stated relation.\nJudge only from the words given.\nOutput ONLY JSON: {"labels": [{"id": int, "label": "keep|drop", "entity": "<name of the related entity it concerns>"}]}'


def _related_labels(data: dict[str, object], count: int) -> dict[int, dict[str, str]]:
    labels: dict[int, dict[str, str]] = {}
    for row in rows(data["labels"]):
        idx, label = row.get("id"), row.get("label")
        if isinstance(idx, int) and 0 <= idx < count and label in ("keep", "drop"):
            labels[idx] = {"label": str(label), "entity": str(row.get("entity") or "")}
    return labels


def classify_related(
    items: list[CollectedItem],
    ticker: str,
    aliases: list[str],
    hits: list[list[Relation]],
    stats: dict[str, float] | None = None,
) -> tuple[dict[int, dict[str, str]], float, str | None]:
    """Two-stage related-entity review (#681/#700): kept only when both stages keep."""

    def content(ids: list[int]) -> str:
        return "\n".join(
            f"{k}\t{ticker} ({', '.join(aliases)})\trelated: "
            + "; ".join(f"{r.name} ({r.relation})" for r in hits[i])
            + f"\t{items[i].title}\t{(items[i].summary or '')[:160]}"
            for k, i in enumerate(ids)
        )

    every = list(range(len(items)))
    data, cost, screen_error = _stage(RELATED_PROMPT, content(every), "screen", stats)
    screen = _related_labels(data, len(items)) if data is not None else {}
    survivors = [i for i in every if screen.get(i, {}).get("label") != "drop"]
    review: dict[int, dict[str, str]] = {}
    review_ok = False
    review_error = None
    if survivors:
        data2, cost2, review_error = _stage(RELATED_PROMPT, content(survivors), "review", stats)
        cost += cost2
        if data2 is not None:
            review_ok = True
            review = {survivors[k]: v for k, v in _related_labels(data2, len(survivors)).items()}
    labels: dict[int, dict[str, str]] = {}
    for i in every:
        first, second = screen.get(i), review.get(i)
        if first and first["label"] == "drop":
            labels[i] = first
        elif (data is not None and first is None) or (review_ok and second is None):
            continue  # a stage that answered left it unlabelled: not kept (#681)
        elif second:
            labels[i] = second
        elif first:
            labels[i] = first
    return labels, cost, _stage_error(screen_error, review_error, data is not None or review_ok)


def classifier_error(exc: Exception) -> str:
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    return f"classifier: {type(exc).__name__}" + (f" HTTP {status}" if status is not None else "")


def _openrouter_once(
    system: str, content: str, model: str, required: str | None
) -> tuple[dict[str, object], float]:
    settings = get_settings()
    with quiet_transport():
        resp = httpx.post(
            settings.OPENROUTER_BASE_URL.rstrip("/") + "/chat/completions",
            headers={
                **OR_ATTRIBUTION_HEADERS,
                "Authorization": "Bearer " + settings.OPENROUTER_API_KEY.get_secret_value(),
            },
            json={
                "model": model,
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
    usage = mapping(data.get("usage") or {})
    reported = usage.get("cost", 0)
    cost = float(reported) if isinstance(reported, (int, float)) else 0.0
    try:
        message = mapping(rows(data["choices"])[0]["message"])
        raw = message["content"]
        if not isinstance(raw, str):
            raise ValueError("invalid classifier content")
        parsed = mapping(parse_llm_json(raw, logger))
        if required is not None:
            rows(parsed[required])  # missing or malformed -> this attempt failed (#700)
        return parsed, cost
    except CLASSIFIER_ERRORS as exc:
        vars(exc)["cost_usd"] = cost  # billed even though the content is unusable
        raise


def openrouter_json(
    system: str,
    content: str,
    model: str | None = None,
    stats: dict[str, float] | None = None,
    required: str | None = None,
) -> tuple[dict[str, object], float]:
    """One classifier-model JSON request with `data_collection: deny`, retried once (#700).

    `required` names a list key the response must carry (the classifiers use
    `labels`; the weekly name check has its own shapes). Raises the second
    failure, carrying the cost of both attempts as `cost_usd`.
    """
    chosen = model or get_settings().INTEL_CLASSIFIER_MODEL
    spent = 0.0
    for attempt in range(2):
        try:
            data, cost = _openrouter_once(system, content, chosen, required)
            return data, spent + cost
        except CLASSIFIER_ERRORS as exc:
            spent += float(getattr(exc, "cost_usd", 0.0))
            if attempt == 0:
                _add(stats, "retries", 1)
                continue
            vars(exc)["cost_usd"] = spent
            raise
    raise AssertionError("unreachable")


class MacroLabel(TypedDict):
    type: str
    importance: int
    event: str
    label_call: NotRequired[str]


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
    label: MacroLabel = {"type": str(kind), "importance": importance, "event": event}
    if isinstance(record.get("label_call"), str):
        label["label_call"] = str(record["label_call"])
    return label


def near_duplicate_title(left: str, right: str, threshold: float) -> bool:
    """Use the existing headline Jaccard rule for cross-call event identity."""
    a, b = tokens(left), tokens(right)
    union = a | b
    return bool(union) and len(a & b) / len(union) >= threshold


MACRO_PROMPT = """You classify public financial news headlines for macro developments.
Judge only the supplied title and summary. Return one label per item:
development = a concrete data release, policy decision, official action or market-moving event;
commentary = opinion, an interested party interview, a promotional letter or a firm's outlook without a new macro development;
off_topic = merely contains a macro keyword, with no macro development (including sports or an individual company's routine contract).
importance = systemic significance: 3 for major economy-wide data, central-bank decisions or broad shocks; 2 for other material macro developments; 1 for limited systemic significance.
event = a short lowercase hyphenated slug describing the specific event. Reports of the same event with no new material fact share a slug. Slugs are comparable only within this request.
Do not provide investment advice. Stay within Layer 3: facts, contextual relationships and observable signals, never instructions, price targets or forecasts.
Output ONLY JSON: {"labels": [{"id": int, "type": "development|commentary|off_topic", "importance": 1|2|3, "event": "short-slug"}]}"""


_MACRO_STRICTNESS = {"development": 0, "commentary": 1, "off_topic": 2}


def _macro_labels(data: dict[str, object], count: int) -> dict[int, MacroLabel]:
    labels: dict[int, MacroLabel] = {}
    for row in rows(data["labels"]):
        idx = row.get("id")
        label = macro_label(row)
        if type(idx) is int and 0 <= idx < count and label is not None:
            labels[idx] = label
    return labels


def classify_macro(
    items: list[CollectedItem], stats: dict[str, float] | None = None
) -> tuple[dict[int, MacroLabel], float, str | None]:
    """Two-stage macro labels (#690/#700): stricter type, lower importance, review slugs."""
    from app.services.intel_body import without_urls

    def content(ids: list[int]) -> str:
        return "\n".join(
            f"{k}\t{without_urls(items[i].title)}\t{without_urls(items[i].summary or '')[:500]}"
            for k, i in enumerate(ids)
        )

    every = list(range(len(items)))
    data, cost, screen_error = _stage(MACRO_PROMPT, content(every), "screen", stats)
    screen = _macro_labels(data, len(items)) if data is not None else {}
    survivors = [i for i in every if i not in screen or screen[i]["type"] != "off_topic"]
    review: dict[int, MacroLabel] = {}
    review_ok = False
    review_error = None
    if survivors:
        data2, cost2, review_error = _stage(MACRO_PROMPT, content(survivors), "review", stats)
        cost += cost2
        if data2 is not None:
            review_ok = True
            review = {survivors[k]: v for k, v in _macro_labels(data2, len(survivors)).items()}
    labels: dict[int, MacroLabel] = {}
    for i in every:
        first, second = screen.get(i), review.get(i)
        if first and second:
            kind = max(first["type"], second["type"], key=_MACRO_STRICTNESS.__getitem__)
            labels[i] = {
                "type": kind,
                "importance": min(first["importance"], second["importance"]),
                "event": second["event"],
            }
        elif second:
            labels[i] = second
        elif first:
            # Slugs are comparable only within one call; keep screen-only slugs apart.
            event = f"s1-{first['event']}" if review_ok else first["event"]
            labels[i] = {**first, "event": event}
    return labels, cost, _stage_error(screen_error, review_error, data is not None or review_ok)
