"""Issue #639 holding recall, prompt rules and merged label-alert acceptance."""

import logging
import uuid
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.models.report import Report
from app.services import report_generator as rg
from app.services import report_prompts as prompts
from app.services.intel_records import link_instrument, store_headline
from app.services.price_anomaly_detector import PriceAnomaly
from app.tasks import report_tasks
from app.tests.conftest import seed_user
from app.tests.test_issue_639_headlines import NOW
from app.tests.test_report_generator import _FAKE_LLM_PASS2, _macro_hit, _news_item, _portfolio_snap

USER = uuid.UUID("00000000-0000-0000-0000-000000000639")


def pipeline(
    stack: ExitStack, snapshot: object, anomalies: list[PriceAnomaly], bodies: list[str]
) -> None:
    for name, value in {
        "compute_portfolio": snapshot,
        "load_news_window": [_news_item("Broad macro development")],
        "detect_macro_signals": _macro_hit(),
        "detect_window_anomalies": (anomalies, 2),
        "resolve_global_moves": ({}, 2),
        "compute_technical_positions": [],
        "_try_assembly": None,
        "_run_shadow_assembly": None,
        "_openrouter_client": None,
    }.items():
        stack.enter_context(patch.object(rg, name, return_value=value))
    stack.enter_context(patch.object(rg, "_call_llm", side_effect=bodies))
    stack.enter_context(patch.object(rg, "_translate_md", side_effect=lambda text, lang: text))


def generate(
    session: Session,
    rows: list[tuple[str, str, int]],
    anomalies: list[PriceAnomaly],
    *,
    headlines_per_holding: int = 3,
) -> Report:
    seed_user(session, USER)
    base = _portfolio_snap()
    holdings = [
        replace(
            base.holdings[0],
            ticker=ticker,
            asset_type=kind,
            market_value_base=Decimal(weight),
            market_value=Decimal(weight),
        )
        for ticker, kind, weight in rows
    ]
    for ticker, _, _ in rows:
        for i in range(headlines_per_holding):
            item = _news_item(f"{ticker} company development {i}")
            item = replace(item, published_at=NOW - timedelta(hours=i))
            nid, _ = store_headline(session, item, "instrument", "article", "keep")
            link_instrument(session, nid, ticker)
    with ExitStack() as stack:
        pipeline(
            stack,
            replace(base, holdings=holdings, total_base=Decimal(100)),
            anomalies,
            [_FAKE_LLM_PASS2],
        )
        return rg.generate_report(session, user_id=USER, now=NOW, output_lang="en")


def anomaly(ticker: str, move: str) -> PriceAnomaly:
    return PriceAnomaly(
        name=ticker,
        identifier=ticker,
        asset_type="STOCK",
        current_price=Decimal(100),
        prev_price=Decimal(100),
        pct_change=Decimal(move),
        window_net_pct=Decimal(move),
        threshold=Decimal(".05"),
    )


def test_639_05_four_percent_stock_has_three_linked_headlines(db_session: Session) -> None:
    report = generate(db_session, [("TSLA", "stock", 4)], [])
    assert report.report_inputs is not None
    assert list(report.report_inputs["holding_news"]) == ["TSLA"]
    assert [item["title"] for item in report.report_inputs["holding_news"]["TSLA"]] == [
        f"TSLA company development {i}" for i in range(3)
    ]


def test_639_05a_eight_stocks_select_six_in_contract_order(db_session: Session) -> None:
    rows = [(f"T{i}", "stock", i + 1) for i in range(8)]
    report = generate(
        db_session, rows, [anomaly("T0", ".08"), anomaly("T1", "-.12")], headlines_per_holding=8
    )
    assert report.report_inputs is not None
    assert set(report.report_inputs["holding_news"]) == {"T1", "T0", "T7", "T6", "T5", "T4"}
    prompt = report.report_inputs["pass2_prompt"].split("=== HOLDING-RELEVANT NEWS", 1)[1]
    identifiers = [
        line[:-1] for line in prompt.splitlines() if line.startswith("T") and line.endswith(":")
    ]
    assert identifiers[:6] == ["T1", "T0", "T7", "T6", "T5", "T4"]
    surfaced_titles = set(
        db_session.scalars(
            select(News.record["title"].astext)
            .join(NewsSurfaced, NewsSurfaced.news_id == News.id)
            .where(NewsSurfaced.user_id == USER)
        )
    )
    discarded_titles = {
        f"{ticker} company development {i}" for ticker in ("T2", "T3") for i in range(8)
    }
    assert sorted(surfaced_titles & discarded_titles) == []
    for ticker in identifiers[:6]:
        assert len(report.report_inputs["holding_news"][ticker]) == 6
        assert {f"{ticker} company development {i}" for i in range(8)} <= surfaced_titles


def test_639_05b_etf_and_anomalous_fund_are_excluded(db_session: Session) -> None:
    report = generate(
        db_session,
        [("QQQM", "etf", 21), ("FUND", "fund", 4), ("TSLA", "stock", 4)],
        [anomaly("FUND", ".12")],
    )
    assert report.report_inputs is not None
    assert list(report.report_inputs["holding_news"]) == ["TSLA"]


def test_639_06_system_rules_forbid_labels_and_missing_data_claims() -> None:
    system = prompts._build_pass2_system()
    assert "Never name input sections or data blocks" in system
    assert "do not mention missing or unavailable data" in system
    assert "make no price-direction claim" in system
    assert "no price data to confirm" not in system


@pytest.mark.parametrize("hits", [True, False])
def test_639_07_three_user_batch_merges_label_alerts(
    db_session: Session, hits: bool, caplog: pytest.LogCaptureFixture
) -> None:
    ids = [uuid.UUID(int=6390 + i) for i in range(3)]
    for user_id in ids:
        seed_user(db_session, user_id)
    bodies = [_FAKE_LLM_PASS2 + ("\nPRICE ANOMALIES" if hits and i < 2 else "") for i in range(3)]
    logging.getLogger(rg.__name__).disabled = False
    with ExitStack() as stack:
        pipeline(stack, _portfolio_snap(), [], bodies)
        stack.enter_context(
            patch(
                "app.core.database.SessionLocal",
                side_effect=lambda: Session(
                    bind=db_session.connection(), join_transaction_mode="create_savepoint"
                ),
            )
        )
        stack.enter_context(
            patch(
                "app.services.user_scope.active_users",
                return_value=[
                    SimpleNamespace(id=uid, locale="en", base_currency="USD") for uid in ids
                ],
            )
        )
        stack.enter_context(patch("app.services.subscription.run_cadence_checks"))
        send = stack.enter_context(patch.object(report_tasks, "send_ops_alert", return_value=True))
        with caplog.at_level(logging.WARNING):
            report_tasks.generate_incremental_report.push_request(id="639-label-batch", retries=0)
            try:
                result = report_tasks.generate_incremental_report.run()
            finally:
                report_tasks.generate_incremental_report.pop_request()
    assert result["status"] == "completed"
    reports = list(
        db_session.scalars(select(Report).where(Report.user_id.in_(ids)).order_by(Report.user_id))
    )
    assert len(reports) == 3 and all(r.status == "success" for r in reports)
    for i, row in enumerate(reports):
        assert row.report_inputs is not None
        assert row.report_inputs["pass2_raw"] == bodies[i]
        assert ("PRICE ANOMALIES" in (row.report_md or "")) == (hits and i < 2)
        assert row.report_inputs.get("prompt_label_hits", []) == (
            ["PRICE ANOMALIES"] if hits and i < 2 else []
        )
    assert send.call_count == int(hits)
    if hits:
        assert send.call_args.kwargs["idempotency_key"] == "report-labels-639-label-batch"
        assert send.call_args.kwargs["severity"] == "WARNING"
        for row in reports[:2]:
            assert str(row.id) in send.call_args.kwargs["body"]
            assert str(row.user_id) in send.call_args.kwargs["body"]
        assert "PRICE ANOMALIES" in caplog.text
