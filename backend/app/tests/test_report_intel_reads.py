from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.cross_name_intel import CrossNameIntel
from app.models.intel import IntelSlotRun
from app.models.macro_event_intel import MacroEventIntel
from app.models.paid_intel import IntelArticle, IntelArticleLink
from app.models.ticker_intel import TickerIntel
from app.services import cross_name_intel, macro_event_intel, ticker_intel
from app.services.report_context import ReportContext
from app.services.report_generator import _load_report_articles
from app.services.report_prompts import _build_pass2_prompt


def test_read_l1_ignores_missing_and_null_or_old_versions(db_session: Session) -> None:
    trade_date = date(2026, 9, 30)
    db_session.add_all(
        [
            TickerIntel(
                identifier="NVDA",
                trade_date=trade_date,
                prompt_version=ticker_intel._PROMPT_VERSION,
                model="test",
                analysis="fresh",
                attempt_count=1,
                facts={},
            ),
            TickerIntel(
                identifier="TSM",
                trade_date=trade_date,
                prompt_version=ticker_intel._PROMPT_VERSION,
                model="test",
                analysis=None,
                attempt_count=1,
                facts={},
            ),
            TickerIntel(
                identifier="OLD",
                trade_date=trade_date,
                prompt_version="old",
                model="test",
                analysis="stale",
                attempt_count=1,
                facts={},
            ),
        ]
    )
    db_session.flush()

    assert ticker_intel.read_l1_intel(db_session, ["NVDA", "TSM", "OLD"], trade_date) == {
        "NVDA": "fresh"
    }


def test_read_l2_and_l3_are_read_only_cache_reads(db_session: Session) -> None:
    trade_date = date(2026, 9, 30)
    db_session.add(
        MacroEventIntel(
            event_key="theme:rates",
            trade_date=trade_date,
            prompt_version=macro_event_intel._PROMPT_VERSION,
            model="test",
            analysis="rates analysis",
            attempt_count=1,
            affected_asset_classes=["STOCK"],
            facts={},
        )
    )
    db_session.add(
        CrossNameIntel(
            trade_date=trade_date,
            prompt_version=cross_name_intel._PROMPT_VERSION,
            input_fingerprint="fp",
            model="test",
            clusters=[{"identifiers": ["NVDA", "TSM"]}],
            attempt_count=1,
            facts={},
        )
    )
    db_session.flush()

    assert macro_event_intel.read_l2_intel(db_session, ["theme:rates"], trade_date) == {
        "theme:rates": {"analysis": "rates analysis", "affected_asset_classes": ["STOCK"]}
    }
    assert cross_name_intel.read_day_synthesis(db_session, trade_date) == [
        {"identifiers": ["NVDA", "TSM"]}
    ]


def test_report_articles_are_windowed_capped_and_user_scoped(db_session: Session) -> None:
    start = datetime(2026, 9, 28, 17, tzinfo=UTC)
    end = datetime(2026, 9, 30, 17, tzinfo=UTC)
    run = IntelSlotRun(
        slot="post_close",
        run_date=date(2026, 9, 30),
        started_at=end - timedelta(minutes=15),
        status="ok",
        details={},
    )
    db_session.add(run)
    db_session.flush()
    ctx = ReportContext(
        portfolio_summary={
            "holdings": [
                {"ticker": "NVDA", "market_value_base": 90},
                {"ticker": "TSM", "market_value_base": 10},
            ],
            "total_base": 100,
        },
        price_anomalies=[{"identifier": "NVDA", "window_net_pct": -0.06}],
        macro_signals={"hits": [{"theme": "energy"}]},
    )
    for identifier, count in (("NVDA", 3), ("TSM", 1), ("OTHER", 1)):
        for index in range(count):
            article = IntelArticle(
                slot_run_id=run.id,
                provider="tavily",
                url_key=f"{identifier}-{index}",
                status="accepted",
                record={
                    "v": 1,
                    "kind": "article",
                    "title": f"{identifier} story {index}",
                    "published_at": None,
                    "fetched_at": end.isoformat(),
                    "body": "body",
                },
                fetched_at=end - timedelta(minutes=index),
            )
            db_session.add(article)
            db_session.flush()
            db_session.add(
                IntelArticleLink(
                    article_id=article.id,
                    identifier=identifier,
                    role="mover",
                )
            )
    db_session.flush()

    entries = _load_report_articles(db_session, ctx, start, end)
    assert [entry["query"] for entry in entries] == ["NVDA", "NVDA", "TSM"]
    assert all("url" not in entry and "provider" not in entry for entry in entries)


def test_background_research_has_no_url_and_uses_long_body() -> None:
    prompt = _build_pass2_prompt(
        {"holdings": [], "total_base": 0},
        {},
        [],
        [
            {
                "index": 1,
                "title": "Headline",
                "published_at": None,
                "content": "x" * 2000,
            }
        ],
    )
    assert "https://" not in prompt
    assert "[S1] Headline (date unknown)" in prompt
    assert "x" * 1500 in prompt
    assert "x" * 1501 not in prompt


def test_historical_report_input_scrub_preserves_non_target_fields() -> None:
    import importlib.util
    from pathlib import Path

    migration_path = (
        Path(__file__).parents[2] / "alembic/versions/d62200000001_scrub_report_inputs.py"
    )
    spec = importlib.util.spec_from_file_location("d622_scrub", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    value = {
        "news_items": [{"title": "A", "url": "https://x", "source": "Feed"}],
        "search_results": [{"title": "B", "url": "https://y", "provider": "Tavily"}],
        "holding_news": {"NVDA": [{"title": "C", "url": "https://z", "source": "Feed"}]},
        "keep": {"url": "https://must-remain"},
    }
    assert migration._scrub(value) == {
        "news_items": [{"title": "A"}],
        "search_results": [{"title": "B", "provider": "Tavily"}],
        "holding_news": {"NVDA": [{"title": "C"}]},
        "keep": {"url": "https://must-remain"},
    }


def test_settings_ignores_removed_report_budget_key() -> None:
    from app.core.config import Settings

    settings = Settings.model_validate({"TAVILY_DAILY_BUDGET": 1})
    assert not hasattr(settings, "TAVILY_DAILY_BUDGET")
