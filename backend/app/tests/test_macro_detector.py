"""Unit tests for macro_detector (E2).

All tests inject keyword_table directly — no YAML file I/O.
No database required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.macro_detector import (
    _load_keywords,
    _make_pattern,
    detect_macro_signals,
)
from app.services.news_fetcher import NewsItem

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 4, 20, 0, 0, tzinfo=UTC)

_SIMPLE_TABLE: dict[str, list[str]] = {
    "货币政策": ["Federal Reserve", "Fed", "rate hike", "美联储"],
    "贸易与关税": ["tariff", "trade war", "关税"],
    "科技监管": ["AI regulation", "AI", "antitrust"],
}


def _item(
    title: str,
    *,
    summary: str | None = None,
    hours_ago: int = 1,
    url: str | None = None,
) -> NewsItem:
    url = url or f"https://example.com/{title.replace(' ', '-').lower()}"
    from app.services.news_fetcher import _url_hash

    return NewsItem(
        url_hash=_url_hash(url),
        title=title,
        url=url,
        source="TEST",
        published_at=_NOW - timedelta(hours=hours_ago),
        summary=summary,
    )


# ---------------------------------------------------------------------------
# _make_pattern — word-boundary vs substring
# ---------------------------------------------------------------------------


def test_make_pattern_single_word_uses_word_boundary() -> None:
    pat = _make_pattern("Fed")
    assert pat.search("The Fed raised rates")
    assert not pat.search("Federal Reserve meeting")  # "Fed" inside "Federal"


def test_make_pattern_phrase_uses_substring() -> None:
    pat = _make_pattern("rate hike")
    assert pat.search("Investors fear another rate hike this month")
    assert pat.search("RATE HIKE expected")  # case-insensitive


def test_make_pattern_chinese_uses_substring() -> None:
    pat = _make_pattern("美联储")
    assert pat.search("今日美联储宣布加息")
    assert pat.search("美联储政策调整")


def test_make_pattern_all_caps_abbreviation_word_boundary() -> None:
    pat = _make_pattern("FOMC")
    assert pat.search("FOMC minutes released")
    assert pat.search("non-FOMC meeting type")  # hyphen = word boundary → still matches (correct)
    assert not pat.search("xFOMCy")  # fused into longer word → no match


def test_make_pattern_ai_single_word_not_inside_word() -> None:
    """'AI' as a single token should not match 'afraid' or 'mail'."""
    pat = _make_pattern("AI")
    assert pat.search("New AI regulation bill passed")
    assert not pat.search("I am afraid of the mail")


def test_make_pattern_ai_regulation_phrase_matches() -> None:
    pat = _make_pattern("AI regulation")
    assert pat.search("Congress debates AI regulation framework")


# ---------------------------------------------------------------------------
# detect_macro_signals — basic hit detection
# ---------------------------------------------------------------------------


def test_no_items_returns_empty_signals() -> None:
    result = detect_macro_signals([], keyword_table=_SIMPLE_TABLE)
    assert result.has_any_hit is False
    assert result.hits == []
    assert result.total_matched_articles == 0


def test_no_match_returns_empty_signals() -> None:
    items = [_item("Local sports news"), _item("Weather update for the weekend")]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)
    assert result.has_any_hit is False
    assert result.total_matched_articles == 0


def test_single_theme_hit() -> None:
    items = [_item("Federal Reserve signals pause in rate hike cycle")]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)

    assert result.has_any_hit is True
    assert result.total_matched_articles == 1
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.theme == "货币政策"
    assert "Federal Reserve" in hit.keywords_found
    assert "rate hike" in hit.keywords_found
    assert len(hit.articles) == 1
    assert hit.articles[0].title == "Federal Reserve signals pause in rate hike cycle"


def test_multiple_themes_hit() -> None:
    items = [
        _item("Fed raises rates; tariff fears mount"),
    ]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)
    themes = {h.theme for h in result.hits}
    assert "货币政策" in themes
    assert "贸易与关税" in themes
    assert result.total_matched_articles == 1  # same article, counted once


def test_same_article_counted_once_across_themes() -> None:
    """An article matching two themes should appear in total_matched_articles once."""
    items = [_item("Fed hikes rates amid tariff threats")]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)
    assert result.total_matched_articles == 1


def test_keywords_found_deduplicated() -> None:
    """Multiple articles can trigger the same keyword; keywords_found is deduplicated."""
    items = [
        _item("Fed pauses", url="https://example.com/1"),
        _item("Fed minutes released", url="https://example.com/2"),
    ]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)
    hit = next(h for h in result.hits if h.theme == "货币政策")
    assert hit.keywords_found.count("Fed") == 1


# ---------------------------------------------------------------------------
# detect_macro_signals — article ordering and cap
# ---------------------------------------------------------------------------


def test_articles_sorted_newest_first() -> None:
    items = [
        _item("Fed news older", hours_ago=5, url="https://example.com/old"),
        _item("Fed news newer", hours_ago=1, url="https://example.com/new"),
        _item("rate hike warning", hours_ago=3, url="https://example.com/mid"),
    ]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)
    hit = next(h for h in result.hits if h.theme == "货币政策")
    assert hit.articles[0].title == "Fed news newer"
    assert hit.articles[1].title == "rate hike warning"
    assert hit.articles[2].title == "Fed news older"


def test_articles_capped_at_max_per_theme() -> None:
    items = [
        _item(f"Fed story {i}", hours_ago=i, url=f"https://example.com/{i}")
        for i in range(1, 6)  # 5 matching articles
    ]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE, max_articles_per_theme=3)
    hit = next(h for h in result.hits if h.theme == "货币政策")
    assert len(hit.articles) == 3


def test_max_articles_per_theme_one() -> None:
    items = [
        _item("Fed meeting", url="https://example.com/a"),
        _item("Fed statement", url="https://example.com/b"),
    ]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE, max_articles_per_theme=1)
    hit = next(h for h in result.hits if h.theme == "货币政策")
    assert len(hit.articles) == 1


# ---------------------------------------------------------------------------
# detect_macro_signals — Chinese keyword matching
# ---------------------------------------------------------------------------


def test_chinese_keyword_matched_in_title() -> None:
    items = [_item("今日美联储宣布加息决定")]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)
    assert result.has_any_hit is True
    hit = result.hits[0]
    assert hit.theme == "货币政策"
    assert "美联储" in hit.keywords_found


def test_chinese_keyword_matched_in_summary() -> None:
    items = [_item("Central bank decision", summary="美联储今日召开会议")]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)
    hit = next((h for h in result.hits if h.theme == "货币政策"), None)
    assert hit is not None
    assert "美联储" in hit.keywords_found


# ---------------------------------------------------------------------------
# detect_macro_signals — summary fallback
# ---------------------------------------------------------------------------


def test_keyword_matched_in_summary_only() -> None:
    """Match in summary when title alone would not trigger."""
    items = [_item("Market update", summary="Investors react to new tariff announcements")]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)
    hit = next((h for h in result.hits if h.theme == "贸易与关税"), None)
    assert hit is not None


def test_none_summary_does_not_raise() -> None:
    items = [_item("Fed raises rates", summary=None)]
    result = detect_macro_signals(items, keyword_table=_SIMPLE_TABLE)
    assert result.has_any_hit is True


# ---------------------------------------------------------------------------
# detect_macro_signals — empty keyword table edge case
# ---------------------------------------------------------------------------


def test_empty_keyword_table_returns_no_hits() -> None:
    items = [_item("Fed raises rates")]
    result = detect_macro_signals(items, keyword_table={})
    assert result.has_any_hit is False
    assert result.hits == []


def test_default_keyword_table_covers_the_widened_candidate_pool() -> None:
    """Regression guard for the 2026-08-21 widening (issue #128 Ring 1 stage
    B / B1 PR): §2's prompt now asks the model to SELECT 2-4 themes with
    genuine change, which is only meaningful if the candidate pool it
    selects from is actually broad. Locks that the original Ring 0 eight
    themes plus the new macro/geopolitical categories both survive — a
    regression here would silently shrink the selection pool back down."""
    # Some theme names use a fullwidth colon, matching macro_keywords.yml's own
    # naming convention — noqa'd per-line below rather than renamed, since
    # renaming here without renaming the YAML keys would just break the match.
    table = _load_keywords()
    for theme in (
        "货币政策",
        "贸易与关税",
        "地缘：中美",  # noqa: RUF001
        "地缘：台海",  # noqa: RUF001
        "地缘：中东",  # noqa: RUF001
        "A股政策",
        "科技监管",
        "宏观：衰退",  # noqa: RUF001
        "科技突破",
        "美债",
        "日债",
        "中国宏观",
        "地缘：俄乌",  # noqa: RUF001
        "地缘：东亚同盟",  # noqa: RUF001
        "G7与全球治理",
        "美国内政",
        "中日摩擦",
    ):
        assert theme in table, f"macro_keywords.yml missing theme: {theme}"
        assert table[theme], f"macro_keywords.yml theme has no keywords: {theme}"


def test_2026_08_22_geopolitics_widening_adds_expected_keywords() -> None:
    """Product owner reviewed a candidate geopolitics keyword list and asked
    for the genuinely new ground (US domestic politics, China-Japan
    friction) plus targeted additions to existing themes — while explicitly
    rejecting the over-generic tokens from that same candidate list ("war",
    bare "nuclear", bare "strait", bare "chips") for the same false-positive
    reason 科技突破's bare "breakthrough"/"突破" were rejected in PR #172.

    English-only (repo Language Policy — this file is not under locales/,
    so pre-existing Chinese keywords elsewhere in it are an accepted-for-now
    gap, not a license to add more): the two new themes and the targeted
    additions below carry no Chinese counterpart, unlike the themes PR #172
    widened."""
    table = _load_keywords()

    assert "Kyiv" in table["地缘：俄乌"]  # noqa: RUF001
    assert "IRGC" in table["地缘：中东"]  # noqa: RUF001
    assert "cross-strait" in table["地缘：台海"]  # noqa: RUF001
    for kw in ("PLA drills", "PLA aircraft", "PLA Navy"):
        assert kw in table["地缘：台海"], f"地缘：台海 missing: {kw}"  # noqa: RUF001

    us_domestic = table["美国内政"]
    for kw in ("Trump", "US Congress", "White House"):
        assert kw in us_domestic, f"美国内政 missing: {kw}"
    assert "特朗普" not in us_domestic
    assert "国会" not in us_domestic
    assert "白宫" not in us_domestic

    japan_friction = table["中日摩擦"]
    for kw in ("Senkaku", "Diaoyu", "East China Sea"):
        assert kw in japan_friction, f"中日摩擦 missing: {kw}"
    assert "尖阁诸岛" not in japan_friction
    assert "钓鱼岛" not in japan_friction
    assert "日元" not in japan_friction

    g7 = table["G7与全球治理"]
    for kw in ("NATO", "EU sanctions", "EU summit", "sanctions coordination"):
        assert kw in g7, f"G7与全球治理 missing: {kw}"

    # Rejected as too generic — same false-positive class as bare
    # "breakthrough"/"突破" (PR #172): "war" fires on trade war/AI arms race/
    # price war headlines already covered elsewhere; bare "nuclear" fires on
    # nuclear-energy/nuclear-physics content; bare "strait" collides with
    # "Strait of Hormuz" (already a phrase in the Middle East theme) and any
    # other strait; bare "chips" fires on potato chips / casino chips; bare
    # "Europe"/"EU"/"yen" (2026-08-22 Grok review round 1, PR #174) fire on
    # routine Europe-market roundups and ordinary Japan-FX copy; bare
    # "sanctions"/"Congress"/"PLA" (round 2) collide with virtually any
    # sanctions headline, National People's Congress / Indian National
    # Congress, and Project Labor Agreement respectively.
    all_keywords = {kw for kws in table.values() for kw in kws}
    for rejected in (
        "war",
        "nuclear",
        "strait",
        "chips",
        "Europe",
        "EU",
        "yen",
        "sanctions",
        "Congress",
        "PLA",
    ):
        assert rejected not in all_keywords, f"rejected over-generic keyword slipped in: {rejected}"


def test_tech_breakthrough_theme_uses_qualified_phrases_not_bare_generic_words() -> None:
    """2026-08-22 Grok review (PR #172): the first widening pass used bare
    "breakthrough" (word-boundary matched, but still fires on any ordinary
    tech-marketing headline containing that word) and bare "突破" (direct
    substring match, fires on completely unrelated financial phrases like
    "股价突破新高"/"订单突破百亿"). Locks that neither bare form survives and
    that the compound, context-qualified replacements are present."""
    keywords = _load_keywords()["科技突破"]
    assert "breakthrough" not in keywords
    assert "突破" not in keywords
    assert "首次实现" not in keywords
    assert "world's first" not in keywords
    for phrase in (
        "scientific breakthrough",
        "research breakthrough",
        "technology breakthrough",
        "科技突破",
        "重大突破",
        "技术突破",
    ):
        assert phrase in keywords, f"macro_keywords.yml missing tightened phrase: {phrase}"
