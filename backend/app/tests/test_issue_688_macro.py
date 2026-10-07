"""Macro ranking and cache-only reader acceptance for #688."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intel import IntelSlotRun
from app.models.paid_intel import IntelArticle, IntelArticleLink
from app.services import headline_cleaning as hc
from app.services import intel_deepen as deepen
from app.services import report_generator as rg
from app.services.instrument_news_sources import CollectedItem
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_digest import build_batch_report
from app.services.intel_leads import select_leads
from app.services.intel_selection import WorkUnit
from app.services.macro_detector import MacroSignals, ThemeHit
from app.services.news_fetcher import NewsItem
from app.services.paid_search import PaidResult
from app.services.report_context import ReportContext
from app.services.report_prompts import _SECTION2_INSTRUCTIONS
from app.tests.test_intel_deepen_rules import NOW
from app.tests.test_intel_deepen_run import factory, settings
from app.tests.test_intel_paid import slot

TRADE = (
    "Trade deficit hits $105.6 billion, widest since just before Trump tariffs enacted last year"
)
ANDURIL = "Anduril lands $2.9 billion Navy submarine shipyard contract days after CEO joins Pentagon weapons group"
GOLF = "LIV Golf secures funding and looks to Trump for help relaunching bankrupt competition"
OIL = "Oil prices stable as market weighs supply risks, rising Middle East exports"
CARMIGNAC = "The era of 'easy money' is over, warns Carmignac boss"
ANCHOR = "Choose the anchor from developments (data, policy, official actions, market events). A single source's opinion or a firm's outlook may support or counter the anchor but is not itself the anchor unless the material contains no development."


def candidates() -> list[CollectedItem]:
    return [
        CollectedItem(t, NOW + timedelta(minutes=i), f"https://source{i}.example/a")
        for i, t in enumerate([TRADE, ANDURIL, GOLF, OIL, OIL])
    ]


def labels() -> list[dict[str, object]]:
    return [
        {"id": i, "type": kind, "importance": score, "event": event}
        for i, (kind, score, event) in enumerate(
            [
                ("development", 3, "trade"),
                ("development", 1, "navy"),
                ("off_topic", 1, "golf"),
                ("development", 2, "oil"),
                ("development", 2, "oil"),
            ]
        )
    ]


def test_ranking_fixture(db_session: Session) -> None:
    assert hasattr(hc, "classify_macro"), "D1 requires the macro classifier helper"
    with patch.object(hc, "openrouter_json", return_value=({"labels": labels()}, 0.001)):
        ranked, cost, error = hc.classify_macro(candidates())
    assert error is None and cost == 0.001
    leads = select_leads(
        db_session,
        WorkUnit("macro", theme="politics"),
        candidates(),
        [],
        load_intel_deepen_config(),
        NOW,
        macro_labels=ranked,
    )
    assert [lead.title for lead in leads] == [TRADE, OIL, ANDURIL]
    assert leads[0].macro_label == {"type": "development", "importance": 3, "event": "trade"}


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"id": 0, "type": "other", "importance": 3, "event": "x"},
        {"id": 0, "type": "development", "importance": 4, "event": "x"},
        {"id": 0, "type": "development", "importance": True, "event": "x"},
        {"id": 0, "type": "development", "importance": 3, "event": ""},
    ],
)
def test_invalid_candidate_is_unlabeled(invalid: dict[str, object], db_session: Session) -> None:
    assert hasattr(hc, "classify_macro"), "D1 requires the macro classifier helper"
    with patch.object(
        hc, "openrouter_json", return_value=({"labels": [invalid, *labels()[1:]]}, 0.001)
    ):
        ranked, _, error = hc.classify_macro(candidates())
    assert error is None and 0 not in ranked
    leads = select_leads(
        db_session,
        WorkUnit("macro", theme="politics"),
        candidates(),
        [],
        load_intel_deepen_config(),
        NOW,
        macro_labels=ranked,
    )
    assert [lead.title for lead in leads] == [OIL, ANDURIL, TRADE]


def test_classifier_url_free_compliance() -> None:
    assert hasattr(hc, "classify_macro"), "D1 requires the macro classifier helper"
    with patch.object(
        hc, "openrouter_json", return_value=({"labels": labels()[:1]}, 0.001)
    ) as request:
        hc.classify_macro(
            [
                CollectedItem(
                    "Data https://private.example/x",
                    NOW,
                    "https://source.example/a",
                    "Summary https://private.example/y",
                )
            ]
        )
    system, content = request.call_args.args
    assert "http" not in content
    assert "Layer 3" in system and "importance" in system


@pytest.mark.parametrize(
    "output,error,failed,partial",
    [
        ({}, "classifier: ReadTimeout", 1, 0),
        ({}, None, 1, 0),
        ({0: {"type": "development", "importance": 3, "event": "trade"}}, None, 0, 4),
    ],
)
def test_fallback_partial_digest(
    db_session: Session,
    output: dict[int, dict[str, object]],
    error: str | None,
    failed: int,
    partial: int,
) -> None:
    assert hasattr(deepen, "classify_macro"), "macro ranking must run before lead selection"
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(deepen, "classify_macro", return_value=(output, 0.001, error)),
        patch.object(deepen, "detect_macro_signals") as detect,
    ):
        w = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(days=1),
            [],
            {},
        )
        w.pool_items = [
            NewsItem(str(i), x.title, x.url, "fixture", x.published_at, x.summary)
            for i, x in enumerate(candidates())
        ]
        run = db_session.get(IntelSlotRun, w.run_id)
        assert run is not None
        run.status = "ok"
        detect.return_value = MacroSignals([ThemeHit("energy", [], w.pool_items)], True, 5)
        batches = []
        with patch.object(
            w, "_extract_batch", side_effect=lambda provider, batch: batches.extend(batch)
        ):
            w.run_wave([WorkUnit("macro", theme="energy", providers=("tavily",))], {}, {})
        w.close()
        details = w.details()
        assert details["macro_rank_failed"] == failed
        assert details["macro_rank_partial"] == partial
        assert w.errors == []
        assert batches[0][1].title == (OIL if failed else TRADE)
        run = db_session.get(IntelSlotRun, w.run_id)
        assert run is not None
        run.details = {"deepening": details}
        body = build_batch_report(db_session, run)[1]
        assert f"macro_rank_failed: {failed}" in body
        assert f"macro_rank_partial: {partial}" in body
        assert run.status == "ok"


@pytest.mark.parametrize("already_extracted", [False, True])
@pytest.mark.parametrize("same_url", [False, True])
def test_cross_unit_extra_theme_link(
    db_session: Session, already_extracted: bool, same_url: bool
) -> None:
    assert hasattr(deepen, "classify_macro"), "macro ranking must run before lead selection"
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
        patch.object(
            deepen,
            "classify_macro",
            side_effect=[
                ({0: {"type": "development", "importance": 2, "event": "oil"}}, 0.0, None),
                ({0: {"type": "development", "importance": 3, "event": "other-slug"}}, 0.0, None),
            ],
        ),
        patch.object(deepen, "detect_macro_signals") as detect,
    ):
        w = deepen.DeepenRun(
            db_session,
            slot(db_session),
            load_intel_deepen_config(),
            False,
            NOW,
            NOW - timedelta(days=1),
            [],
            {},
        )
        w.pool_items = [
            NewsItem(str(i), x.title, x.url, "fixture", x.published_at, x.summary)
            for i, x in enumerate(candidates()[3:])
        ]
        if same_url:
            w.pool_items[1] = replace(
                w.pool_items[1],
                url=w.pool_items[0].url,
                title="Distinct title describing the identical URL",
            )
        detect.return_value = MacroSignals(
            [
                ThemeHit("energy", [], w.pool_items[:1]),
                ThemeHit("geopolitics", [], w.pool_items[1:]),
            ],
            True,
            2,
        )
        with patch.object(
            w,
            "_call",
            return_value=(
                "tavily",
                PaidResult(
                    units=Decimal(1),
                    cost_usd=Decimal(".008"),
                    bodies={
                        w.pool_items[0].url: "Oil supply data and export levels changed this week. "
                        * 40
                    },
                    http_status=200,
                ),
            ),
        ) as paid:
            units = [
                WorkUnit("macro", theme=t, providers=("tavily",)) for t in ["energy", "geopolitics"]
            ]
            if already_extracted:
                w.run_wave(units[:1], {}, {})
                w.run_wave(units[1:], {}, {})
            else:
                w.run_wave(units, {}, {})
        w.close()
        assert paid.call_count == 1
        articles = list(db_session.scalars(select(IntelArticle)))
        assert len(articles) == 1 and articles[0].status == "accepted"
        assert articles[0].record is not None and articles[0].record["event"] == "oil"
        assert set(db_session.scalars(select(IntelArticleLink.theme))) == {"energy", "geopolitics"}


def test_reader_importance_cap_legacy_cross_call_slug(db_session: Session) -> None:
    run = slot(db_session)
    stories = [
        (CARMIGNAC, "rates", None),
        ("BOJ policy decision", "rates", 2),
        (OIL, "energy", 2),
        (OIL + "!", "energy", 2),
        ("Paramount deal closes", "tech", 1),
        (ANDURIL, "politics", 1),
        (TRADE, "politics", 3),
        ("Older legacy body", "politics", None),
    ]
    for i, (title, theme, importance) in enumerate(stories):
        record: dict[str, object] = {
            "title": title,
            "body": "body",
            "published_at": NOW.isoformat(),
        }
        if importance:
            record.update(type="development", importance=importance, event="same-slug-across-calls")
        a = IntelArticle(
            slot_run_id=run.id,
            provider="tavily",
            url_key=str(i),
            status="accepted",
            record=record,
            fetched_at=NOW - timedelta(minutes=i),
        )
        db_session.add(a)
        db_session.flush()
        db_session.add(IntelArticleLink(article_id=a.id, theme=theme, role="macro"))
    db_session.flush()
    ctx = ReportContext(
        portfolio_summary={"holdings": [], "total_base": 0},
        macro_signals={"hits": [{"theme": t} for t in ["rates", "energy", "tech", "politics"]]},
    )
    with patch.object(
        hc, "openrouter_json", side_effect=AssertionError("report reader cannot classify")
    ):
        for _ in range(3):
            entries = rg._load_report_articles(db_session, ctx, NOW - timedelta(days=1), NOW)
            titles = [e["title"] for e in entries]
            assert titles[0] == TRADE
            assert sum(t.startswith(OIL) for t in titles) == 1
            assert len(titles) == 6 and titles[-1] == CARMIGNAC
            assert "BOJ policy decision" in titles


def test_anchor_sentence() -> None:
    assert ANCHOR in " ".join(_SECTION2_INSTRUCTIONS.split())
    assert "ONE deep anchor" in _SECTION2_INSTRUCTIONS
    assert "0-2 short independent updates" in _SECTION2_INSTRUCTIONS


def test_three_user_reports_keep_one_pass2_call_each(db_session: Session) -> None:
    import contextlib
    import uuid
    from typing import cast

    from app.tests.conftest import seed_user
    from app.tests.test_report_generator import _TODAY, _mock_llm, _normal_path_patches

    with contextlib.ExitStack() as stack:
        for patcher in _normal_path_patches():
            stack.enter_context(cast(contextlib.AbstractContextManager[object], patcher))
        with (
            patch.object(rg, "_call_llm", side_effect=_mock_llm) as llm,
            patch.object(
                hc, "openrouter_json", side_effect=AssertionError("report path cannot classify")
            ),
        ):
            for _ in range(3):
                user_id = uuid.uuid4()
                seed_user(db_session, user_id)
                report = rg.generate_report(
                    db_session, user_id=user_id, report_date=_TODAY, output_lang="en"
                )
                assert report.status == "success"
        assert llm.call_count == 3


def test_all_commentary_off_topic_use_no_lead_slots(db_session: Session) -> None:
    with patch.object(
        hc,
        "openrouter_json",
        return_value=(
            {
                "labels": [
                    {
                        "id": i,
                        "type": "commentary" if i % 2 else "off_topic",
                        "importance": 1,
                        "event": f"event-{i}",
                    }
                    for i in range(5)
                ]
            },
            0.001,
        ),
    ):
        ranked, _, error = hc.classify_macro(candidates())
    assert error is None and len(ranked) == 5
    assert (
        select_leads(
            db_session,
            WorkUnit("macro", theme="politics"),
            candidates(),
            [],
            load_intel_deepen_config(),
            NOW,
            macro_labels=ranked,
        )
        == []
    )
