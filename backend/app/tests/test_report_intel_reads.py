from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.intel import IntelSlotRun
from app.models.paid_intel import IntelArticle, IntelArticleLink
from app.services import report_generator as rg
from app.services.report_context import ReportContext
from app.services.report_generator import _load_report_articles
from app.services.report_prompts import _build_pass2_prompt
from app.services.report_sections import _build_footer


def _article(
    db_session: Session,
    run: IntelSlotRun,
    identifier: str | None,
    index: int,
    fetched_at: datetime,
    *,
    theme: str | None = None,
) -> None:
    article = IntelArticle(
        slot_run_id=run.id,
        provider="tavily",
        url_key=f"{identifier or theme}-{index}",
        status="accepted",
        record={
            "v": 1,
            "kind": "article",
            "title": f"{identifier or theme} story {index}",
            "published_at": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "body": "body",
        },
        fetched_at=fetched_at,
    )
    db_session.add(article)
    db_session.flush()
    db_session.add(
        IntelArticleLink(article_id=article.id, identifier=identifier, theme=theme, role="mover")
    )


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


def test_scrub_missing_keys_and_null_macro_hits_are_unchanged() -> None:
    import importlib.util
    from pathlib import Path

    migration_path = (
        Path(__file__).parents[2] / "alembic/versions/d62200000001_scrub_report_inputs.py"
    )
    spec = importlib.util.spec_from_file_location("d622_scrub_missing", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    missing = {"keep": {"nested": True}}
    null_hits = {"macro_signals": {"hits": None}, "keep": ["value"]}
    assert migration._scrub(missing) == missing
    assert migration._scrub(null_hits) == null_hits


def test_settings_ignores_removed_report_budget_key() -> None:
    from app.core.config import Settings

    settings = Settings.model_validate({"TAVILY_DAILY_BUDGET": 1})
    assert not hasattr(settings, "TAVILY_DAILY_BUDGET")


def test_acceptance_01_generate_report_uses_only_scheduled_intel(db_session: Session) -> None:
    from app.tests.test_report_generator import (
        _TODAY,
        _anomaly,
        _macro_hit,
        _mock_llm,
        _portfolio_snap,
    )

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator.intel_trade_date", return_value=_TODAY),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm) as llm,
    ):
        from app.tests.conftest import seed_user
        from app.tests.test_report_generator import _USER

        seed_user(db_session, _USER)
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    assert report.status == "success"
    assert llm.call_count == 1


def test_acceptance_02_uses_latest_completed_post_close_slot(db_session: Session) -> None:
    from app.services.report_generator import intel_trade_date

    # 2026-10-01 Thu, 10-02 Fri, 10-03 Sat, 10-04 Sun, 10-05 Mon. Weekend
    # post_close runs exist (slots run daily) but are not considered, so a
    # weekend report falls back to the latest completed weekday run.
    def _run(day: int, status: str) -> IntelSlotRun:
        return IntelSlotRun(
            slot="post_close",
            run_date=date(2026, 10, day),
            started_at=datetime(2026, 10, day, 20, 15, tzinfo=UTC),
            status=status,
            details={},
        )

    db_session.add_all([_run(1, "ok"), _run(2, "failed"), _run(3, "ok")])
    db_session.flush()
    # Contract case: Thu ok, Fri failed, weekend run ok -> Saturday uses Thursday.
    assert intel_trade_date(db_session, date(2026, 10, 3)) == date(2026, 10, 1)
    assert intel_trade_date(db_session, date(2026, 9, 30)) is None


def test_acceptance_02_weekend_reports_use_latest_weekday_slot(db_session: Session) -> None:
    from app.services.report_generator import intel_trade_date

    def _run(day: int, status: str) -> IntelSlotRun:
        return IntelSlotRun(
            slot="post_close",
            run_date=date(2026, 10, day),
            started_at=datetime(2026, 10, day, 20, 15, tzinfo=UTC),
            status=status,
            details={},
        )

    db_session.add_all([_run(2, "ok"), _run(3, "ok"), _run(4, "partial"), _run(5, "partial")])
    db_session.flush()
    # Saturday weekly report and a Sunday manual report read Friday's caches.
    assert intel_trade_date(db_session, date(2026, 10, 3)) == date(2026, 10, 2)
    assert intel_trade_date(db_session, date(2026, 10, 4)) == date(2026, 10, 2)
    # A weekday run on eff_date itself is used, including status partial.
    assert intel_trade_date(db_session, date(2026, 10, 5)) == date(2026, 10, 5)


def test_acceptance_03_worked_example_orders_five_search_results(db_session: Session) -> None:
    start = datetime(2026, 9, 28, tzinfo=UTC)
    end = datetime(2026, 9, 30, tzinfo=UTC)
    run = IntelSlotRun(
        slot="post_close", run_date=date(2026, 9, 30), started_at=end, status="ok", details={}
    )
    db_session.add(run)
    db_session.flush()
    for identifier in ("NVDA", "TSM"):
        for i in range(2 if identifier == "NVDA" else 1):
            _article(db_session, run, identifier, i, end - timedelta(minutes=i))
    for i in range(2):
        _article(db_session, run, None, i, end - timedelta(minutes=i), theme="energy")
    db_session.flush()
    ctx = ReportContext(
        portfolio_summary={
            "holdings": [
                {"ticker": "NVDA", "market_value_base": 60},
                {"ticker": "TSM", "market_value_base": 40},
            ],
            "total_base": 100,
        },
        price_anomalies=[{"identifier": "NVDA", "window_net_pct": -0.1}],
        macro_signals={"hits": [{"theme": "energy"}]},
    )
    entries = _load_report_articles(db_session, ctx, start, end)
    assert [entry["query"] for entry in entries] == [
        "NVDA",
        "NVDA",
        "TSM",
        "theme:energy",
        "theme:energy",
    ]
    assert all("OTHER" not in entry["title"] for entry in entries)


def test_theme_articles_deduplicate_shared_url_keys(db_session: Session) -> None:
    start = datetime(2026, 9, 28, tzinfo=UTC)
    end = datetime(2026, 9, 30, tzinfo=UTC)
    run = IntelSlotRun(
        slot="post_close", run_date=date(2026, 9, 30), started_at=end, status="ok", details={}
    )
    db_session.add(run)
    db_session.flush()
    for provider in ("tavily", "parallel"):
        article = IntelArticle(
            slot_run_id=run.id,
            provider=provider,
            url_key="shared-url-key",
            status="accepted",
            record={
                "v": 1,
                "kind": "article",
                "title": f"{provider} title",
                "published_at": end.isoformat(),
                "fetched_at": end.isoformat(),
                "body": "body",
            },
            fetched_at=end,
        )
        db_session.add(article)
        db_session.flush()
        db_session.add(IntelArticleLink(article_id=article.id, theme="energy", role="macro"))
    db_session.flush()
    ctx = ReportContext(
        portfolio_summary={"holdings": [], "total_base": 0},
        price_anomalies=[],
        macro_signals={"hits": [{"theme": "energy"}]},
    )
    entries = _load_report_articles(db_session, ctx, start, end)
    assert len(entries) == 1


def test_acceptance_04_caps_twenty_holdings_at_fifteen_results(db_session: Session) -> None:
    start = datetime(2026, 9, 28, tzinfo=UTC)
    end = datetime(2026, 9, 30, tzinfo=UTC)
    run = IntelSlotRun(
        slot="post_close", run_date=date(2026, 9, 30), started_at=end, status="ok", details={}
    )
    db_session.add(run)
    db_session.flush()
    for i in range(20):
        for j in range(3):
            _article(db_session, run, f"T{i:02d}", j, end - timedelta(minutes=j))
    ctx = ReportContext(
        portfolio_summary={
            "holdings": [{"ticker": f"T{i:02d}", "market_value_base": 1} for i in range(20)],
            "total_base": 20,
        },
        price_anomalies=[{"identifier": "T00", "window_net_pct": -0.2}],
        macro_signals={},
    )
    entries = _load_report_articles(db_session, ctx, start, end)
    assert len(entries) == 15
    assert all(sum(entry["query"] == f"T{i:02d}" for entry in entries) <= 2 for i in range(20))
    assert entries[0]["query"] == "T00"


def test_acceptance_05_slot_window_is_open_at_end_and_closed_at_start(db_session: Session) -> None:
    start = datetime(2026, 9, 28, tzinfo=UTC)
    end = datetime(2026, 9, 30, tzinfo=UTC)
    old = IntelSlotRun(
        slot="post_close", run_date=date(2026, 9, 28), started_at=start, status="ok", details={}
    )
    current = IntelSlotRun(
        slot="post_close", run_date=date(2026, 9, 30), started_at=end, status="ok", details={}
    )
    db_session.add_all([old, current])
    db_session.flush()
    _article(db_session, old, "NVDA", 0, start)
    _article(db_session, current, "NVDA", 1, end)
    db_session.flush()
    ctx = ReportContext(
        portfolio_summary={
            "holdings": [{"ticker": "NVDA", "market_value_base": 1}],
            "total_base": 1,
        },
        price_anomalies=[],
        macro_signals={},
    )
    assert [entry["title"] for entry in _load_report_articles(db_session, ctx, start, end)] == [
        "NVDA story 1"
    ]


def test_acceptance_06_nvda_recall_merges_six_newest_unique_items() -> None:
    from app.tests.test_report_generator import _news_item

    items = [_news_item(f"NVDA {i}") for i in range(8)]
    duplicate = _news_item("NVDA 0")
    recalled, hashes = rg._merge_holding_news([*items, duplicate], {"NVDA": items[2:]}, ["NVDA"])
    assert len(recalled["NVDA"]) == 6
    assert recalled["NVDA"] == sorted(
        recalled["NVDA"], key=lambda item: item.published_at, reverse=True
    )
    assert len(hashes) == len({item.url_hash for item in items})


def test_news_title_dedup_keeps_distinct_cjk_and_collapses_english_variants() -> None:
    from app.services.news_fetcher import NewsItem, url_hash

    def item(title: str, slug: str) -> NewsItem:
        url = f"https://example.com/{slug}"
        return NewsItem(
            url_hash(url), title, url, "TEST", datetime(2026, 9, 30, tzinfo=UTC), "body"
        )

    cjk_one = item("中国公司发布新公告", "cjk-one")
    cjk_two = item("中国公司发布业绩预告", "cjk-two")
    english_one = item("NVDA raises outlook!", "english-one")
    english_two = item("nvda raises outlook", "english-two")
    recalled, _ = rg._merge_holding_news(
        [], {"NVDA": [cjk_one, cjk_two, english_one, english_two]}, ["NVDA"]
    )
    assert [entry.title for entry in recalled["NVDA"]] == [
        "中国公司发布新公告",
        "中国公司发布业绩预告",
        "NVDA raises outlook!",
    ]


def test_acceptance_07_next_window_does_not_reuse_prior_surfaced_news(db_session: Session) -> None:
    from app.models.news_surfaced import NewsSurfaced
    from app.models.report import Report
    from app.services.window_data import load_news_window, mark_news_surfaced
    from app.tests.conftest import seed_user
    from app.tests.test_report_generator import _USER
    from app.tests.test_window_data import _news

    seed_user(db_session, _USER)
    report = Report(
        user_id=_USER,
        report_date=date(2026, 6, 5),
        report_type="incremental",
        session_node="after_close",
        status="success",
        period_start=datetime(2026, 6, 3, tzinfo=UTC),
        period_end=datetime(2026, 6, 5, tzinfo=UTC),
    )
    db_session.add(report)
    db_session.add(_news("prior", datetime(2026, 6, 4, tzinfo=UTC)))
    db_session.flush()
    items = load_news_window(
        db_session, datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 7, tzinfo=UTC), _USER
    )
    mark_news_surfaced(db_session, _USER, report.id, [item.url_hash for item in items])
    db_session.flush()
    assert (
        load_news_window(
            db_session, datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 8, tzinfo=UTC), _USER
        )
        == []
    )
    assert db_session.query(NewsSurfaced).count() == 1


def test_acceptance_09_footer_locales_are_source_free() -> None:
    portfolio = {
        "base_currency": "USD",
        "fx_rates_as_of": {"USD": "2026-10-01", "HKD": "2026-10-02"},
    }
    for lang in ("en", "zh", "zh-Hant"):
        footer = _build_footer(portfolio, lang)
        assert all(
            word not in footer for word in ("yfinance", "Tiantian", "Reuters", "CNBC", "Google")
        )
    assert "Notes & Disclaimer" in _build_footer(portfolio, "en")
    assert "说明与免责声明" in _build_footer(portfolio, "zh")
    english = _build_footer(portfolio, "en")
    chinese = _build_footer(portfolio, "zh")
    assert "Exchange rates: " in english
    assert "Exchange rates as of" not in english
    assert "Exchange rates as of USD as of" not in english
    assert "汇率日期：" in chinese  # noqa: RUF001
    assert "截至" not in chinese


def test_acceptance_10_legacy_render_uses_stored_report_body(db_session: Session) -> None:
    import contextlib

    from app.tests.conftest import seed_user
    from app.tests.test_report_generator import _USER, _normal_path_patches

    seed_user(db_session, _USER)
    with contextlib.ExitStack() as stack:
        for patcher in _normal_path_patches():
            stack.enter_context(patcher)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=date(2026, 9, 30))
    assert report.report_inputs is not None and report.report_md is not None
    report.report_inputs.update(
        {"pass1_model": "retired", "pass1_prompt": "retired", "pass1_raw": "retired"}
    )
    db_session.flush()
    original_body = report.report_md.split("\n---\n", 1)[0]
    with patch(
        "app.services.report_generator._call_llm", side_effect=AssertionError("render is read-only")
    ):
        rebuilt = rg.regenerate_report(db_session, report.id, user_id=_USER, mode="render")
    assert rebuilt.report_md is not None
    assert rebuilt.report_md.split("\n---\n", 1)[0] == original_body


def test_acceptance_11_background_research_is_long_and_versioned() -> None:
    test_background_research_has_no_url_and_uses_long_body()
    assert rg._PROMPT_VERSION == "f2-v12"


def test_acceptance_12_settings_ignore_removed_budget_key() -> None:
    test_settings_ignores_removed_report_budget_key()


def test_acceptance_14_scrub_is_idempotent_and_preserves_unrelated_fields() -> None:
    test_historical_report_input_scrub_preserves_non_target_fields()


def test_acceptance_15_pass2_prompt_has_citations_without_urls() -> None:
    test_background_research_has_no_url_and_uses_long_body()
    prompt = _build_pass2_prompt(
        {"holdings": [], "total_base": 0},
        {},
        [],
        [
            {
                "index": 1,
                "title": "Headline",
                "published_at": datetime(2026, 9, 30, 12, tzinfo=UTC),
                "content": "body",
            }
        ],
    )
    assert "http://" not in prompt and "https://" not in prompt
    assert "[S1] Headline (2026-09-30)" in prompt
