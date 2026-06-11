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
