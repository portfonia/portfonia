"""Detect macro keyword theme hits in a list of news articles (Ring 0 — Stage E2).

Design decisions:
- Keyword table lives in backend/config/macro_keywords.yml (editable without
  code changes). Path overrideable via MACRO_KEYWORDS_PATH env var.
- Matching rules (auto-inferred from keyword structure, not user-configured):
    Chinese characters present → direct substring match (no word boundaries
      in Chinese text).
    English phrase (contains space) → case-insensitive substring.
    Single English token (no space) → \\b word-boundary regex so "Fed" does
      not match "Federal" and "AI" does not match "afraid".
- detect_macro_signals() accepts an optional keyword_table argument so tests
  inject an inline dict and never touch the filesystem.
- Articles are capped at max_articles_per_theme (default 3) per theme,
  newest-first. This keeps LLM Pass 1 prompt size predictable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.config import get_settings
from app.services.news_fetcher import NewsItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config path resolution
# ---------------------------------------------------------------------------

# backend/ = two levels above this file (services/macro_detector.py → app/ → backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_KEYWORDS_FILE = _BACKEND_DIR / "config" / "macro_keywords.yml"


def _get_keywords_path() -> Path:
    override = get_settings().MACRO_KEYWORDS_PATH
    return Path(override) if override else _DEFAULT_KEYWORDS_FILE


def _load_keywords(path: Path | None = None) -> dict[str, list[str]]:
    """Load keyword table from YAML. Returns {theme_name: [keyword, ...]}."""
    actual = path or _get_keywords_path()
    with actual.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return {t["name"]: [str(k) for k in t["keywords"]] for t in data["themes"]}


# ---------------------------------------------------------------------------
# Pattern compilation
# ---------------------------------------------------------------------------

_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def _make_pattern(keyword: str) -> re.Pattern[str]:
    """
    Compile a regex for a single keyword based on its structure.

    - Contains Chinese characters → plain substring (case-insensitive).
    - Contains a space (English phrase) → plain substring (case-insensitive).
    - Single English token → word-boundary (\\b) to avoid substring false positives.
    """
    if _CHINESE_RE.search(keyword) or " " in keyword:
        return re.compile(re.escape(keyword), re.IGNORECASE)
    return re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ThemeHit:
    """One triggered macro theme with supporting evidence."""

    theme: str
    keywords_found: list[str]  # which keywords matched (deduplicated)
    articles: list[NewsItem]  # matching articles, newest-first, capped


@dataclass
class MacroSignals:
    """Aggregated macro signal output for a news batch."""

    hits: list[ThemeHit]  # only triggered themes (empty = quiet day)
    has_any_hit: bool
    total_matched_articles: int  # unique articles that matched any theme


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_macro_signals(
    items: list[NewsItem],
    keyword_table: dict[str, list[str]] | None = None,
    max_articles_per_theme: int = 3,
) -> MacroSignals:
    """
    Scan `items` for macro keyword hits.

    Args:
        items: News articles from fetch_news() (E1).
        keyword_table: Optional override for testing; if None, loads from the
            configured YAML file.
        max_articles_per_theme: Maximum articles kept per theme hit (newest-first).

    Returns:
        MacroSignals with only the triggered themes populated in `hits`.
    """
    table = keyword_table if keyword_table is not None else _load_keywords()

    # Pre-compile all patterns once.
    compiled: dict[str, list[tuple[str, re.Pattern[str]]]] = {
        theme: [(kw, _make_pattern(kw)) for kw in keywords] for theme, keywords in table.items()
    }

    hits: list[ThemeHit] = []
    matched_url_hashes: set[str] = set()

    for theme, kw_patterns in compiled.items():
        theme_articles: list[NewsItem] = []
        keywords_found: list[str] = []

        for item in items:
            search_text = item.title + " " + (item.summary or "")
            matched = [kw for kw, pat in kw_patterns if pat.search(search_text)]
            if matched:
                theme_articles.append(item)
                matched_url_hashes.add(item.url_hash)
                for kw in matched:
                    if kw not in keywords_found:
                        keywords_found.append(kw)

        if theme_articles:
            theme_articles.sort(key=lambda x: x.published_at, reverse=True)
            hits.append(
                ThemeHit(
                    theme=theme,
                    keywords_found=keywords_found,
                    articles=theme_articles[:max_articles_per_theme],
                )
            )
            logger.info(
                "theme '%s': %d article(s), keywords: %s",
                theme,
                len(theme_articles),
                keywords_found,
            )

    signals = MacroSignals(
        hits=hits,
        has_any_hit=bool(hits),
        total_matched_articles=len(matched_url_hashes),
    )
    logger.info(
        "detect_macro_signals: %d theme(s) hit, %d unique article(s) matched",
        len(hits),
        signals.total_matched_articles,
    )
    return signals
