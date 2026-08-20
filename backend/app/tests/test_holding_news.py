"""Tests for holding-relevant news recall (R-3)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.holding_news import recall_holding_news
from app.services.news_fetcher import NewsItem

_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def _item(title: str, summary: str = "") -> NewsItem:
    url = f"https://example.com/{title.replace(' ', '-').lower()}"
    return NewsItem(
        url_hash=url[-16:],
        title=title,
        url=url,
        source="TEST",
        published_at=_NOW,
        summary=summary or None,
    )


def test_recall_matches_by_ticker_word_boundary() -> None:
    news = [_item("INTC jumps on new order"), _item("Printing press upgrade")]
    out = recall_holding_news(news, ["INTC"], keyword_table={})
    assert [i.title for i in out["INTC"]] == ["INTC jumps on new order"]


def test_recall_matches_by_alias_not_containing_ticker() -> None:
    # The BoJ/EWJ miss: thematically relevant, but no ticker in the headline.
    news = [_item("BoJ governor Ueda hospitalised")]
    table = {"EWJ": ["Bank of Japan", "BOJ", "BoJ", "Ueda"]}
    out = recall_holding_news(news, ["EWJ"], keyword_table=table)
    assert out["EWJ"][0].title == "BoJ governor Ueda hospitalised"


def test_recall_omits_holdings_with_no_match() -> None:
    news = [_item("Unrelated market chatter")]
    out = recall_holding_news(news, ["NVDA"], keyword_table={"NVDA": ["Nvidia"]})
    assert out == {}


def test_recall_caps_items_per_holding() -> None:
    news = [_item(f"Intel news {i}") for i in range(5)]
    out = recall_holding_news(news, ["INTC"], keyword_table={"INTC": ["Intel"]}, max_per_holding=2)
    assert len(out["INTC"]) == 2


def test_recall_searches_summary_as_well_as_title() -> None:
    news = [_item("Quiet headline", summary="A note on Qualcomm's modem roadmap")]
    out = recall_holding_news(news, ["QCOM"], keyword_table={"QCOM": ["Qualcomm"]})
    assert out["QCOM"][0].title == "Quiet headline"


def test_recall_word_boundary_avoids_substring_false_positive() -> None:
    # "gold" must not match "Goldman"; \b after "gold" blocks it.
    news = [_item("Goldman Sachs ups forecast")]
    out = recall_holding_news(news, ["SGOL"], keyword_table={"SGOL": ["gold"]})
    assert out == {}


# ---------------------------------------------------------------------------
# Ranking (issue #128 quality gate, design doc §6.7 item 3)
#
# The observed failure: TSM's recall on 2026-08-17 returned a generic AI
# filing that mentioned a supply-chain term in its body, while the cap kept a
# headline actually about TSMC out. Recency alone is the wrong sort key when
# the budget is 3 items — a body mention is much weaker evidence than a title
# mention, and it was winning purely by being newer.
# ---------------------------------------------------------------------------


def test_title_match_outranks_a_body_only_match() -> None:
    news = [
        _item("AI infrastructure bets keep piling up", summary="Analysts cite TSMC capacity"),
        _item("TSMC lifts capex guidance"),
    ]
    out = recall_holding_news(news, ["TSM"], keyword_table={"TSM": ["TSMC"]}, max_per_holding=1)
    assert [i.title for i in out["TSM"]] == ["TSMC lifts capex guidance"]


def test_ranking_does_not_discard_the_weaker_match_when_room_remains() -> None:
    """Ranking reorders; it must not shrink the result set. A body-only match
    is weaker evidence, not noise."""
    news = [
        _item("AI infrastructure bets keep piling up", summary="Analysts cite TSMC capacity"),
        _item("TSMC lifts capex guidance"),
    ]
    out = recall_holding_news(news, ["TSM"], keyword_table={"TSM": ["TSMC"]}, max_per_holding=3)
    assert len(out["TSM"]) == 2


def test_recency_still_breaks_ties_within_the_same_rank() -> None:
    older = _item("TSMC lifts capex guidance")
    newer = _item("TSMC signs a new foundry deal")
    # `recall_holding_news` is handed news already sorted newest-first by the
    # window loader; equal-strength matches must preserve that order.
    out = recall_holding_news(
        [newer, older], ["TSM"], keyword_table={"TSM": ["TSMC"]}, max_per_holding=2
    )
    assert [i.title for i in out["TSM"]] == [newer.title, older.title]
