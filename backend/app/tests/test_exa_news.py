"""Focused issue #812 contracts; every external provider is mocked."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import ET
from app.models.paid_intel import IntelArticle, IntelArticleLink, PaidApiUsage
from app.services import intel_deepen as deepen
from app.services.agent_pull import pull_intel
from app.services.instrument_news_sources import CollectedItem
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_digest import build_batch_report
from app.services.intel_leads import url_key
from app.services.intel_records import build_article_record
from app.services.intel_selection import WorkUnit, select_units
from app.services.macro_detector import _load_keywords, detect_macro_signals
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_agent_pull import report
from app.tests.test_intel_paid import slot

NOW = datetime(2026, 10, 20, 18, tzinfo=ET)
BODY = (
    "The company announced a production agreement that expands capacity and deliveries this year. "
    * 8
)
COUNTS = {
    "Monetary policy": 9,
    "Semiconductors": 8,
    "Magnificent 7": 7,
    "Middle East": 5,
    "US Treasuries": 4,
    "Recession": 3,
    "AI infrastructure and capex": 2,
}


def test_01_config() -> None:
    cfg = load_intel_deepen_config()
    assert cfg.sector_themes and set(cfg.sector_themes) <= _load_keywords().keys()
    assert cfg.caps.sector_macro_themes == 3
    assert cfg.exa.num_results == 20
    data = cfg.model_dump()
    del data["exa"]["queries"]["sector"]
    with pytest.raises(ValidationError):
        type(cfg).model_validate(data)


def test_02_weekday() -> None:
    assert [u.theme for u in select_units({}, COUNTS, load_intel_deepen_config())] == [
        "Monetary policy",
        "Semiconductors",
        "Magnificent 7",
        "Middle East",
        "US Treasuries",
        "AI infrastructure and capex",
    ]


def test_03_weekend() -> None:
    cfg = load_intel_deepen_config()
    empty = cfg.model_copy(update={"sector_themes": []})
    # Access the new field so this test first fails before the #812 config exists.
    assert len(cfg.sector_themes) == 3
    assert select_units({}, COUNTS, cfg, weekend=True) == select_units(
        {}, COUNTS, empty, weekend=True
    )


def test_04_keywords() -> None:
    def themes(title: str) -> set[str]:
        return {
            h.theme
            for h in detect_macro_signals(
                [CollectedItem(title, NOW, "https://cnbc.com/a").headline()]
            ).hits
        }

    assert themes("TSMC") == {"Semiconductors"}
    assert themes("AI capex") == {"AI infrastructure and capex"}
    assert "Magnificent 7" in themes("Nvidia")
    assert not themes("blue chip stocks") & {
        "Semiconductors",
        "AI infrastructure and capex",
        "Magnificent 7",
    }


def test_05_request() -> None:
    from app.services import exa_search

    cfg = load_intel_deepen_config().exa
    settings = get_settings().model_copy(update={"EXA_API_KEY": SecretStr("fixture")})
    with patch.object(
        exa_search, "post", return_value=httpx.Response(200, json={"results": []})
    ) as post:
        exa_search.search(
            settings,
            cfg.queries["sector"],
            cfg.domains,
            NOW - timedelta(hours=24),
            cfg.num_results,
            cfg.max_characters,
        )
    assert post.call_count == 1
    assert post.call_args.args == ("https://api.exa.ai/search",)
    assert post.call_args.kwargs == {
        "headers": {"x-api-key": "fixture"},
        "timeout": 60,
        "json": {
            "query": cfg.queries["sector"],
            "type": "auto",
            "category": "news",
            "numResults": 20,
            "includeDomains": cfg.domains,
            "startPublishedDate": "2026-10-19T22:00:00.000Z",
            "contents": {"text": {"maxCharacters": 4000}},
        },
    }


def hit(
    title: str, path: str, *, text: str = BODY, stamp: str | None = "2026-10-20T18:00:00Z"
) -> dict[str, object]:
    return {"url": "https://cnbc.com/" + path, "title": title, "publishedDate": stamp, "text": text}


@contextmanager
def worker(
    db: Session, hits: list[dict[str, object]], *, failure: bool = False
) -> Iterator[tuple[deepen.DeepenRun, Mock]]:
    from app.services import exa_search

    settings = get_settings().model_copy(
        update={"EXA_API_KEY": SecretStr("fixture"), "INTEL_PAID_WORKERS": 1}
    )

    def classify(
        items: list[CollectedItem], **kwargs: object
    ) -> tuple[dict[int, dict[str, object]], float, str | None]:
        if failure:
            return {}, 0.012, "classifier: fixture failure"
        return (
            {
                i: {
                    "type": "commentary" if item.title == "Opinion column" else "development",
                    "importance": 2,
                    "event": f"event-{i}",
                }
                for i, item in enumerate(items)
            },
            0.012,
            None,
        )

    with (
        patch.object(deepen, "get_settings", return_value=settings),
        patch.object(
            deepen,
            "SessionLocal",
            side_effect=lambda: Session(
                bind=db.connection(), join_transaction_mode="create_savepoint"
            ),
        ),
        patch.object(deepen, "classify_macro", side_effect=classify),
        patch.object(
            exa_search,
            "post",
            side_effect=[
                httpx.Response(200, json={"results": hits, "costDollars": {"total": 0.017}}),
                httpx.Response(200, json={"results": []}),
            ],
        ) as post,
    ):
        run = slot(db)
        run.run_date = NOW.date()
        run.started_at = NOW
        db.flush()
        w = deepen.DeepenRun(
            db, run, load_intel_deepen_config(), False, NOW, NOW - timedelta(days=1), [], {}
        )
        try:
            yield w, post
        finally:
            w.close()


def test_06_dates(db_session: Session) -> None:
    from app.services import exa_search

    settings = get_settings().model_copy(update={"EXA_API_KEY": SecretStr("fixture")})
    data = [
        hit("Date only", "a", stamp="2026-10-20T00:00:00.000Z"),
        hit("Timestamp", "b", stamp="2026-10-21T01:02:36.000Z"),
    ]
    with patch.object(exa_search, "post", return_value=httpx.Response(200, json={"results": data})):
        result = exa_search.search(settings, "fixture", [], NOW, 20, 4000)
    assert result.results[0].published_at == datetime(2026, 10, 20, tzinfo=ET)
    assert result.results[1].published_at is not None
    assert result.results[1].published_at.astimezone(ET) == datetime(
        2026, 10, 20, 21, 2, 36, tzinfo=ET
    )
    with worker(db_session, [hit("Missing", "missing", stamp=None)]) as (w, _):
        w.run_exa()
    assert db_session.scalar(select(IntelArticle.reject_reason)) == "undated"


def test_07_pipeline(db_session: Session) -> None:
    old = slot(db_session)
    old.run_date = NOW.date() - timedelta(days=2)
    db_session.add(
        IntelArticle(
            slot_run_id=old.id,
            provider="tavily",
            url_key=url_key("https://cnbc.com/seen"),
            status="accepted",
            record=build_article_record("Previous", NOW, NOW, BODY),
            fetched_at=NOW - timedelta(days=2),
        )
    )
    db_session.flush()
    hits = [
        hit("Missing date", "undated", stamp=None),
        hit("Previously accepted", "seen"),
        hit("Factory opens production facility", "kept"),
        hit("Factory opens production facility", "duplicate"),
        hit(
            "Short announcement",
            "short",
            text="The company announced a production agreement expanding capacity this year. " * 2,
        ),
        hit("Opinion column", "opinion"),
        hit("Court approves settlement", "second"),
    ]
    with worker(db_session, hits) as (w, post):
        w.run_exa()
        details = cast(Any, w.details())["exa"]
    rows = db_session.scalars(select(IntelArticle).where(IntelArticle.provider == "exa")).all()
    assert sum(r.status == "accepted" for r in rows) == 2
    assert {r.reject_reason for r in rows if r.status == "rejected"} == {
        "undated",
        "too_short",
        "commentary",
    }
    assert len(rows) == 5 and all(r.news_id is None for r in rows)
    assert details["queries"][0]["rejected"] == {
        "undated": 1,
        "seen_recently": 1,
        "duplicate": 1,
        "too_short": 1,
        "commentary": 1,
    }
    assert post.call_count == 2
    assert all("http" not in str(r.record) for r in rows if r.record)


def test_08_classifier_failure(db_session: Session) -> None:
    with worker(db_session, [hit("Factory agreement", "fallback")], failure=True) as (w, _):
        w.run_exa()
        assert "classifier: fixture failure" in cast(Any, w.details())["errors"]
        assert w.macro_classifier_cost_usd == pytest.approx(0.012)
    assert db_session.scalar(select(IntelArticle.status)) == "accepted"


def test_09_links(db_session: Session) -> None:
    with worker(
        db_session,
        [
            hit("Acme capacity expansion", "holding", text="HBM " + BODY),
            hit("Court approves settlement", "other"),
        ],
    ) as (w, _):
        w.aliases = {"ACME": ["Acme"]}
        w.run_exa()
        links = cast(Any, w.details())["exa"]["links"]
    rows = db_session.scalars(select(IntelArticleLink)).all()
    assert {(r.identifier, r.theme, r.role) for r in rows} == {
        ("ACME", None, "exa"),
        (None, "Semiconductors", "exa"),
        (None, "other", "exa"),
    }
    assert links == {"holding": 1, "theme": 1, "other": 1}


def test_10_batch_dedup(db_session: Session) -> None:
    from app.models.news import News

    rss_theme = next(theme for theme, keywords in _load_keywords().items() if "tariff" in keywords)
    with worker(db_session, [hit("Federal Reserve approves new facility", "shared")]) as (w, _):
        w.run_exa()
        item = CollectedItem(
            "Federal Reserve approves new facility",
            NOW,
            "https://cnbc.com/shared",
            summary="New tariff agreement",
        )
        db_session.add(
            News(
                url_hash=item.headline().url_hash,
                origin="pool",
                published_at=NOW,
                record={"title": item.title},
            )
        )
        db_session.flush()
        w.pool_items = [item.headline()]
        with patch.object(w, "_call", side_effect=AssertionError("No extraction")) as call:
            w.run_wave([WorkUnit("macro", theme=rss_theme)], {}, {})
        call.assert_not_called()
    assert (
        db_session.scalar(select(IntelArticleLink.id).where(IntelArticleLink.theme == rss_theme))
        is not None
    )


def test_11_usage(db_session: Session) -> None:
    with worker(db_session, []) as (w, post):
        w.run_exa()
        ledger = db_session.scalars(select(PaidApiUsage)).all()
        assert len(ledger) == 2 and all(
            r.provider == "exa" and r.operation == "search" for r in ledger
        )
        assert ledger[0].cost_usd == Decimal("0.017") and ledger[0].units == 1
        post.reset_mock()
        w.usage.month_used["exa"] = w.settings.EXA_MONTHLY_USD_LIMIT
        w.run_exa()
        post.assert_not_called()
        w.settings.EXA_API_KEY = None
        w.run_exa()
        post.assert_not_called()
        assert not w.errors


def test_12_agent(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.timezones.today_et", lambda: NOW.date())
    seed_user(db_session, TEST_USER_ID)
    report(db_session, NOW.date(), created_at=NOW, themes=["Semiconductors"])
    with worker(
        db_session,
        [hit("Court approves settlement", "other"), hit("HBM capacity expansion", "theme")],
    ) as (w, _):
        w.run_exa()
    theme_id = db_session.scalar(
        select(IntelArticle.id).where(IntelArticle.url_key == url_key("https://cnbc.com/theme"))
    )
    db_session.add(IntelArticleLink(article_id=theme_id, theme="other", role="exa"))
    db_session.flush()
    jade = pull_intel(db_session, TEST_USER_ID, NOW.date(), since=None, full=True)
    assert [a.title for a in jade.other] == ["Court approves settlement"]
    assert jade.macro and [a.title for a in jade.macro.themes[0].articles] == [
        "HBM capacity expansion"
    ]
    basic = pull_intel(db_session, TEST_USER_ID, NOW.date(), since=None, full=False)
    assert basic.other == [] and basic.macro and basic.macro.themes[0].articles == []


def test_13_report_read(db_session: Session) -> None:
    from app.services.report_generator import _load_report_articles

    with worker(db_session, [hit("Court approves settlement", "other")]) as (w, _):
        w.run_exa()
    ctx = cast(
        Any,  # Only the fields consumed by the article reader are needed here.
        SimpleNamespace(
            portfolio_summary={},
            price_anomalies=[],
            macro_signals={"hits": [{"theme": "Semiconductors"}]},
        ),
    )
    assert (
        _load_report_articles(db_session, ctx, NOW - timedelta(days=1), NOW + timedelta(seconds=1))
        == []
    )


def test_14_email(db_session: Session) -> None:
    run = slot(db_session)
    run.details = {
        "deepening": {
            "exa": {
                "configured": True,
                "month": 1.23,
                "month_limit": 5.0,
                "queries": [
                    {
                        "name": "sector",
                        "outcome": "ok",
                        "results": 19,
                        "accepted": 11,
                        "rejected": {
                            "commentary": 3,
                            "too_short": 2,
                            "undated": 1,
                            "seen_recently": 1,
                            "duplicate": 1,
                        },
                        "cost_usd": 0.017,
                    }
                ],
                "links": {"holding": 7, "theme": 9, "other": 2},
            }
        }
    }
    import re

    body = build_batch_report(db_session, run)[1].split("PART 2")[1]
    numbers = re.findall(r"\d+(?:\.\d+)?", body)
    assert all(
        n in numbers for n in ["19", "11", "3", "2", "1", "0.017", "7", "9", "1.23", "5.00", "25"]
    )
