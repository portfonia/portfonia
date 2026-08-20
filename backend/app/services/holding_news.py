"""Holding-relevant news recall (R-3, mapping gap).

After ``detect_window_anomalies`` flags the holdings that moved, this module
recalls news captured in the report window that is relevant to each flagged
holding — by ticker (always) plus per-holding entity/theme aliases from
``config/holding_news_keywords.yml``.

Why this exists: Pass 1 only sees macro themes + the top-15 headlines, and only
articles matching a macro theme reach Pass 2. A holding-specific story that
matches no macro theme (observed: "BoJ governor Ueda hospitalised" → EWJ) was
captured in the ``news`` table but never reached the analysis layer, so the
holding's move went unexplained. This recall is a pure code-level keyword match
over the ALREADY-loaded window news — no live fetch, no LLM — and its output is
supplied only to Pass 2 (the identifiers are holdings-derived).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from app.core.config import get_settings
from app.services.macro_detector import _make_pattern
from app.services.news_fetcher import NewsItem

logger = logging.getLogger(__name__)

# backend/ = two levels above this file (services/holding_news.py → app/ → backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_KEYWORDS_FILE = _BACKEND_DIR / "config" / "holding_news_keywords.yml"

# Cap recalled items per holding so a noisy alias cannot flood the Pass 2 prompt.
_MAX_NEWS_PER_HOLDING = 3


def _get_keywords_path() -> Path:
    override = get_settings().HOLDING_NEWS_KEYWORDS_PATH
    return Path(override) if override else _DEFAULT_KEYWORDS_FILE


def load_holding_keywords(path: Path | None = None) -> dict[str, list[str]]:
    """Load the per-holding alias table. Returns {identifier: [alias, ...]}."""
    actual = path or _get_keywords_path()
    if not actual.exists():
        logger.warning("holding news keyword file not found: %s", actual)
        return {}
    with actual.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    holdings = data.get("holdings", {}) or {}
    return {str(k): [str(a) for a in (v or [])] for k, v in holdings.items()}


def recall_holding_news(
    news_items: list[NewsItem],
    identifiers: list[str],
    keyword_table: dict[str, list[str]] | None = None,
    max_per_holding: int = _MAX_NEWS_PER_HOLDING,
) -> dict[str, list[NewsItem]]:
    """Recall window news relevant to each given holding identifier.

    For every identifier we match the ticker itself (word-boundary) plus any
    aliases configured for it. A news item matches when the term appears in its
    title or summary. Identifiers with no match are omitted from the result.

    RANKING (issue #128 quality gate, design doc §6.7 item 3): matches are
    ordered TITLE-MATCHES-FIRST, then by the order they arrive in
    ``news_items`` (already newest-first from the window loader), and only then
    capped. Recency alone was the sort key until the 2026-08-17 comparison
    showed what that costs at a cap of 3: TSM's recall returned a generic AI
    piece that mentioned a supply-chain term in its body, while a headline
    actually about TSMC never made the cut — the weaker match won purely by
    being newer. A term in the headline is what the story is ABOUT; a term in
    the body may be a passing reference, and the difference decides whether L1
    gets a company catalyst or a [Speculative] shrug.

    Ranking reorders, it never discards: a body-only match still lands in the
    result whenever the cap leaves room. The whole scan runs before the cap is
    applied (the previous version stopped at the first ``max_per_holding``
    hits, which is why a later, stronger match could never displace an
    earlier, weaker one).
    """
    table = keyword_table if keyword_table is not None else load_holding_keywords()
    result: dict[str, list[NewsItem]] = {}

    for ident in identifiers:
        # The ticker is always a recall term; aliases add entity/theme coverage.
        terms = [ident, *table.get(ident, [])]
        patterns = [_make_pattern(t) for t in terms if t]
        # (rank, arrival index, item) — rank 0 = the term is in the headline.
        # `arrival` keeps the loader's newest-first order as the tiebreaker
        # within a rank, and makes the sort deterministic for equal keys.
        scored: list[tuple[int, int, NewsItem]] = []
        for arrival, item in enumerate(news_items):
            in_title = any(p.search(item.title) for p in patterns)
            in_summary = any(p.search(item.summary or "") for p in patterns)
            if in_title:
                scored.append((0, arrival, item))
            elif in_summary:
                scored.append((1, arrival, item))
        if scored:
            scored.sort(key=lambda entry: (entry[0], entry[1]))
            result[ident] = [item for _rank, _arrival, item in scored[:max_per_holding]]

    return result
