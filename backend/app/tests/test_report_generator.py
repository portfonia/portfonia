"""Tests for report_generator orchestration (generate_report/regenerate_report).

Strategy:
- All external calls (LLM, Tavily, news_fetcher, macro_detector,
  price_anomaly_detector, email_sender) are mocked via patch.
- DB operations use the db_session fixture (real Postgres).
- Tests cover: normal path, quiet-day skip, LLM failure, Tavily failure (degraded).

Split from a single file into per-module test files (#37) — this file keeps
only tests exercising generate_report/regenerate_report/_render_full_md/
_is_short_manual_quiet end-to-end. Unit tests for the individual pieces
(prompts, code-built sections, the LLM transport, search, translation,
serializers, the compliance scan, report_inputs types) moved to
test_report_prompts.py, test_report_sections.py, test_report_llm.py,
test_report_translation.py, test_report_serializers.py, test_output_scan.py,
and test_report_context.py respectively.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.forward_event import ForwardEvent
from app.models.holding import Holding
from app.models.report import Report
from app.services import report_generator as rg
from app.services import section3_proportionality as s3p
from app.services.intel_records import build_headline_record
from app.services.macro_detector import MacroSignals, ThemeHit
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import (
    Concentration,
    HoldingValue,
    PortfolioSnapshot,
)
from app.services.price_anomaly_detector import PriceAnomaly
from app.services.window_data import HoldingMove, MovesCache
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-000000000099")
_NOW = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)
_TODAY = date(2026, 6, 4)


@pytest.fixture(autouse=True)
def _seed_test_user(db_session: Session) -> None:
    """issue #129 B7's new FKs need a `users` row for _USER before most
    tests here write a holding/report under it. A few tests
    (`_seed_investment_context`) insert their own `User(id=_USER, ...)` row
    afterward with specific fields (locale/intel_focus) — those sites now
    check-first and skip their own insert if this fixture already created
    one, rather than the two racing on the same primary key."""
    seed_user(db_session, _USER)


# ---------------------------------------------------------------------------
# Module-level guard: block real email delivery in every test in this file.
# Without this, any test that reaches step 10 of generate_report() hits the
# live Resend API and sends an actual email.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_email() -> MagicMock:  # type: ignore[misc]
    with patch("app.services.report_generator.send_report_email") as mock:
        mock.return_value = True
        yield mock


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _news_item(title: str) -> NewsItem:
    from app.services.news_fetcher import url_hash

    url = f"https://example.com/{title.replace(' ', '-').lower()}"
    return NewsItem(
        url_hash=url_hash(url),
        title=title,
        url=url,
        source="TEST",
        published_at=_NOW,
        summary=f"Summary of {title}",
    )


def _macro_hit() -> MacroSignals:
    item = _news_item("Fed raises rates")
    hit = ThemeHit(
        theme="货币政策",
        keywords_found=["Fed"],
        articles=[item],
    )
    return MacroSignals(hits=[hit], has_any_hit=True, total_matched_articles=1)


def _quiet_signals() -> MacroSignals:
    return MacroSignals(hits=[], has_any_hit=False, total_matched_articles=0)


def _anomaly() -> PriceAnomaly:
    return PriceAnomaly(
        name="NVIDIA",
        identifier="NVDA",
        asset_type="stock",
        current_price=Decimal("120.0"),
        prev_price=Decimal("110.0"),
        pct_change=Decimal("0.0909"),
        threshold=Decimal("0.03"),
    )


def _portfolio_snap() -> PortfolioSnapshot:
    hv = HoldingValue(
        holding_id=uuid.uuid4(),
        name="Apple Inc.",
        ticker="AAPL",
        fund_code=None,
        currency="USD",
        asset_type="stock",
        asset_class="STOCK",
        market="US",
        market_value=Decimal("10000"),
        market_value_base=Decimal("10000"),
        price_as_of=_NOW,
    )
    return PortfolioSnapshot(
        base_currency="USD",
        holdings=[hv],
        total_base=Decimal("10000"),
        by_currency={"USD": Decimal("10000")},
        by_asset_type={"stock": Decimal("10000")},
        by_market={"US": Decimal("10000")},
        by_asset_class={"STOCK": Decimal("10000")},
        concentration=Concentration(
            top_holding_name="Apple Inc.",
            top_holding_ratio=Decimal("1.0"),
            top_holding_asset_class="STOCK",
            top3_ratio=Decimal("1.0"),
            top_asset_class_name="STOCK",
            top_asset_class_ratio=Decimal("1.0"),
            single_holding_watch=True,
            single_holding_high=True,
            top3_watch=True,
            asset_class_watch=True,
            asset_class_high=True,
        ),
        stale_tickers=[],
    )


# Padding so fake Pass 2 bodies clear _PASS2_MIN_CHARS (H-DEBT-2 completeness
# guard) — real Pass 2 output runs several thousand chars across §2/§3/§4.
_PASS2_FILLER = "Filler context. " * 130

_FAKE_LLM_PASS1 = (
    '{"queries": ["Federal Reserve rate decision impact", "NVIDIA earnings semiconductor"]}'
)
_FAKE_LLM_PASS2 = (
    "## §2 Macro Events\n\nFed raised rates. [For information only — not investment advice]\n\n"
    "## §3 Holdings Intelligence\n\nNVIDIA up 9%. [For information only — not investment advice]\n\n"
    "## §4 Exposure & Price Data\n\nConcentration watch. [For information only — not investment advice]\n\n"
    + _PASS2_FILLER
)

# F2-specific fake: includes [S#] citations and AAPL references to exercise annotations.
_FAKE_LLM_PASS2_F2 = (
    "## §2 Macro Events\n\n"
    "Fed raised rates significantly according to recent reports [S1]. "
    "[For information only — not investment advice]\n\n"
    "## §3 Holdings Intelligence\n\n"
    "AAPL represents a large portion of the portfolio and is sensitive to rate changes. "
    "[For information only — not investment advice]\n\n"
    "## §4 Exposure & Price Data\n\n"
    "Concentration above thresholds. [For information only — not investment advice]\n\n"
    + _PASS2_FILLER
)

_FAKE_TAVILY_RESULTS = [
    {
        "query": "Federal Reserve rate decision impact",
        "title": "Fed holds rates",
        "url": "https://reuters.com/fed",
        "content": "The Federal Reserve kept rates unchanged...",
        "score": 0.9,
        "index": 1,
    }
]


def _mock_llm(
    client: object,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
    **kwargs: object,
) -> str:
    if with_holdings:
        return _FAKE_LLM_PASS2
    return _FAKE_LLM_PASS1


# ---------------------------------------------------------------------------
# Tests: normal path
# ---------------------------------------------------------------------------


def test_generate_report_normal_path(db_session: Session) -> None:
    """Full pipeline: macro hit + anomaly → scheduled reads → Pass 2 → DB write."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_date == _TODAY
    assert report.report_type == "incremental"
    assert report.report_md is not None
    assert "§1 Portfolio Snapshot" in report.report_md
    assert "§2 Macro Events" in report.report_md
    assert "§3 Holdings Intelligence" in report.report_md
    assert "§4 Exposure & Price Data" in report.report_md
    assert report.generated_at is not None
    assert report.report_inputs is not None
    assert report.report_inputs["pass2_model"] != ""
    assert report.report_inputs["search_results"] == []


def test_generate_report_macro_sidecar_stripped_and_coverage_persisted(
    db_session: Session,
) -> None:
    """issue #440: the macro-coverage sidecar the §2-writing pass appends
    must never reach the rendered report_md, and its parsed items must be
    persisted to `macro_coverage`, keyed by this report's id."""
    from sqlalchemy import select as sa_select

    from app.models.macro_coverage import MacroCoverage
    from app.services import macro_coverage as mc

    sidecar = (
        f"{mc._SIDECAR_START}\n"
        '{"items": [{"development_key": "Fed Rate Path", "coverage_mode": "NEW", '
        '"depth_tier": "anchor", "open_questions": ["will cuts continue?"], '
        '"affected_identifiers": ["AAPL"]}]}'
        f"\n{mc._SIDECAR_END}"
    )

    def _mock_llm_with_sidecar(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kwargs: object,
    ) -> str:
        if with_holdings:
            return _FAKE_LLM_PASS2 + sidecar
        return _FAKE_LLM_PASS1

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_with_sidecar),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_md is not None
    assert "MACRO_COVERAGE" not in report.report_md
    assert "development_key" not in report.report_md

    rows = (
        db_session.execute(sa_select(MacroCoverage).where(MacroCoverage.report_id == report.id))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].development_key == "fed rate path"
    assert rows[0].depth_tier == "anchor"
    assert rows[0].user_id == _USER


def test_generate_report_empty_book_content_contract(db_session: Session) -> None:
    """issue #221 §2.7 (Ring 1-Onboarding.md): a user with no
    user_investment_context row and no holdings still gets a completed
    report — this is a content contract on the existing empty-list code
    path, not a new pipeline. §1 renders its headers over an empty table
    (no crash on division by a zero total). §2.5 still lists a
    holdings-independent scheduled event (FOMC/CPI-style, ticker="") with
    Exposed holdings rendered as "—" rather than omitted or crashing.
    No UserInvestmentContext row is seeded — Pass 2 falls back to the B1
    system default framework, which is already the existing behavior."""
    empty_portfolio = PortfolioSnapshot(base_currency="USD")
    db_session.add(
        ForwardEvent(
            event_type="macro",
            name="FOMC Meeting",
            ticker="",
            scheduled_date=_TODAY + timedelta(days=1),
            source="fomc",
        )
    )
    db_session.flush()

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=empty_portfolio),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator.intel_trade_date", return_value=_TODAY),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_md is not None
    assert "§1 Portfolio Snapshot" in report.report_md
    assert "USD 0" in report.report_md  # zero total, no ZeroDivisionError
    assert "§2.5 Forward Calendar" in report.report_md
    assert "FOMC Meeting" in report.report_md
    # No holding to expose it to -> "—", not omitted or crashed.
    assert "| FOMC Meeting | —" in report.report_md


def test_generate_report_retry_clears_stale_provider_message_id(db_session: Session) -> None:
    """issue #45 review follow-up: a row reused for retry (status not success/
    skipped) must clear provider_message_id alongside email_sent_at — otherwise
    a previously-sent report can carry a stale Resend id into its next attempt
    while email_sent_at reads NULL, breaking the "both set or both unset"
    invariant the pair is meant to hold."""

    def _mock_pipeline() -> list[contextlib.AbstractContextManager[object]]:
        return [
            patch(
                "app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()
            ),
            patch(
                "app.services.report_generator.load_news_window",
                return_value=[_news_item("Fed raises rates")],
            ),
            patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
            patch(
                "app.services.report_generator.detect_window_anomalies",
                return_value=([_anomaly()], 2),
            ),
            patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
            patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        ]

    with contextlib.ExitStack() as stack:
        for cm in _mock_pipeline():
            stack.enter_context(cm)
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"

    # Simulate a prior real send, then a state that makes the row eligible for
    # reset on the next generate_report() call (anything not success/skipped).
    report.status = "needs_review"
    report.email_sent_at = _NOW
    report.provider_message_id = "resend-stale-id-from-prior-send"
    db_session.commit()

    with contextlib.ExitStack() as stack:
        for cm in _mock_pipeline():
            stack.enter_context(cm)
        retried = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert retried.id == report.id
    assert retried.status == "success"
    # send_report_email is mocked (module-level _no_email fixture) and never
    # touches provider_message_id, so a None here proves the reset branch
    # cleared it rather than it being silently repopulated by a real send.
    assert retried.provider_message_id is None


# ---------------------------------------------------------------------------
# Tests: stage-skip on retry (#61) — a retry must not redo Pass 1 + Pass 2
# when the failed row already carries a complete body, and must not redo
# ANYTHING for a success-but-unsent-email row.
# ---------------------------------------------------------------------------


_REJECTED_PASS2 = "x" * 3000


@pytest.fixture
def rejected_pass2_llm() -> Generator[MagicMock, None, None]:
    """Mock external inputs while keeping orchestration and DB persistence real."""
    with (
        patch.object(rg, "compute_portfolio", return_value=_portfolio_snap()),
        patch.object(rg, "load_news_window", return_value=[_news_item("Fed raises rates")]),
        patch.object(rg, "detect_macro_signals", return_value=_macro_hit()),
        patch.object(rg, "detect_window_anomalies", return_value=([_anomaly()], 2)),
        patch.object(rg, "_openrouter_client", return_value=MagicMock()),
        patch.object(rg, "_call_llm", side_effect=[_REJECTED_PASS2]) as llm,
    ):
        yield llm


def _generate_rejected_pass2(db_session: Session) -> Report:
    with pytest.raises(RuntimeError, match="Pass 2 output looks truncated") as exc:
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    row = db_session.execute(
        select(Report).where(Report.user_id == _USER, Report.report_date == _TODAY)
    ).scalar_one()
    db_session.refresh(row)
    assert (
        str(exc.value)
        == f"report {row.id}: Pass 2 output looks truncated (3000 chars, missing one of §3/§4)"
    )
    assert row.status == "failed"
    assert row.report_inputs is not None
    assert row.report_inputs["pass2_raw"] == ""
    assert row.report_md is None
    assert row.email_sent_at is None
    return row


def test_rejected_pass2_persists_failed_output(
    db_session: Session, rejected_pass2_llm: MagicMock, _no_email: MagicMock
) -> None:
    row = _generate_rejected_pass2(db_session)
    assert rejected_pass2_llm.call_count == 1
    _no_email.assert_not_called()
    assert row.report_inputs is not None
    assert row.report_inputs["rejected_pass2_raw"] == _REJECTED_PASS2


def test_rejected_pass2_retry_reruns_without_resume(
    db_session: Session, rejected_pass2_llm: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    row = _generate_rejected_pass2(db_session)
    assert row.report_inputs is not None
    assert row.report_inputs["rejected_pass2_raw"] == _REJECTED_PASS2
    assert row.prompt_version == rg._PROMPT_VERSION
    assert row.disclaimer_version == rg._DISCLAIMER_VERSION
    rejected_pass2_llm.reset_mock(side_effect=True)
    rejected_pass2_llm.side_effect = [_FAKE_LLM_PASS2]
    logging.getLogger(rg.__name__).disabled = False
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=rg.__name__):
        retried = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    db_session.refresh(retried)
    assert retried.id == row.id
    assert retried.status == "success"
    assert "resuming from stored Pass 2/assembly body" not in caplog.text
    assert rejected_pass2_llm.call_count == 1
    assert rejected_pass2_llm.call_args.kwargs["with_holdings"] is True
    assert retried.report_inputs is not None
    assert retried.report_inputs["pass2_raw"] == _FAKE_LLM_PASS2
    assert retried.report_inputs["rejected_pass2_raw"] == ""


def test_rejected_pass2_regenerate_has_no_stored_body(
    db_session: Session, rejected_pass2_llm: MagicMock
) -> None:
    row = _generate_rejected_pass2(db_session)
    assert row.report_inputs is not None
    assert row.report_inputs["rejected_pass2_raw"] == _REJECTED_PASS2
    rejected_pass2_llm.reset_mock()
    for mode in ("render", "analyze"):
        with pytest.raises(ValueError, match="has no stored report body"):
            rg.regenerate_report(db_session, row.id, user_id=_USER, mode=mode)
    rejected_pass2_llm.assert_not_called()
    db_session.refresh(row)
    assert row.status == "failed"
    assert row.report_inputs["rejected_pass2_raw"] == _REJECTED_PASS2


def test_rejected_pass2_success_has_no_rejected_content(
    db_session: Session, rejected_pass2_llm: MagicMock
) -> None:
    rejected_pass2_llm.side_effect = [_FAKE_LLM_PASS2]
    report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    db_session.refresh(report)
    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["pass2_raw"] == _FAKE_LLM_PASS2
    assert report.report_inputs["rejected_pass2_raw"] == ""


@pytest.mark.parametrize("legacy", [False, True])
def test_generate_report_retry_after_render_failure_skips_pass1_pass2(
    db_session: Session, _no_email: MagicMock, legacy: bool
) -> None:
    """#61: Pass 2 succeeds, translation raises -> the failed row's report_inputs
    already carries a complete pass2_raw (persisted by the outer except
    handler). A retry must resume straight from render using that stored
    body instead of redoing Pass 1 + Pass 2 (a real, costly LLM call) and
    re-fetching the portfolio/news/anomalies inputs those passes needed."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm) as mock_llm,
        patch(
            "app.services.report_generator._translate_md",
            side_effect=RuntimeError("translation boom"),
        ),
        patch("app.services.report_generator.mark_news_surfaced") as first_mark,
        pytest.raises(RuntimeError, match="translation boom"),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    first_mark.assert_not_called()

    assert mock_llm.call_count == 1  # Pass 2 ran exactly once

    row = db_session.execute(
        select(Report).where(Report.user_id == _USER, Report.report_date == _TODAY)
    ).scalar_one()
    assert row.status == "failed"
    assert row.report_inputs is not None
    assert row.report_inputs.get("pass2_raw")
    pool_hash = _news_item("Fed raises rates").url_hash
    holding_hash = "holding-only-hash"
    stored_inputs = dict(row.report_inputs)
    stored_item = dict(stored_inputs["news_items"][0])
    stored_item["url_hash"] = holding_hash
    stored_inputs["holding_news"] = {"NVDA": [stored_item]}
    row.report_inputs = stored_inputs
    db_session.commit()

    if legacy:
        inputs = dict(row.report_inputs)
        inputs["news_items"] = [
            {**n, "url": _news_item("Fed raises rates").url} for n in inputs["news_items"]
        ]
        for n in inputs["news_items"]:
            n.pop("url_hash")
        row.report_inputs = inputs
        db_session.commit()

    with (
        patch("app.services.report_generator.mark_news_surfaced") as mark,
        patch("app.services.report_generator.compute_portfolio") as mock_portfolio2,
        patch("app.services.report_generator.load_news_window") as mock_news2,
        patch("app.services.report_generator.detect_macro_signals") as mock_macro2,
        patch("app.services.report_generator.detect_window_anomalies") as mock_anom2,
        patch("app.services.report_generator._openrouter_client") as mock_client2,
        patch("app.services.report_generator._call_llm") as mock_llm2,
    ):
        retried = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert retried.id == row.id
    assert retried.status == "success"
    assert retried.report_md is not None
    assert "§4 Exposure & Price Data" in retried.report_md
    mock_llm2.assert_not_called()
    mock_client2.assert_not_called()
    mock_portfolio2.assert_not_called()
    mock_news2.assert_not_called()
    mock_macro2.assert_not_called()
    mock_anom2.assert_not_called()

    assert mark.call_args.args[3] == ([holding_hash] if legacy else [pool_hash, holding_hash])


def test_generate_report_retry_after_prompt_version_bump_reruns_pass1_pass2(
    db_session: Session, _no_email: MagicMock
) -> None:
    """#61: the stage-skip reuse gate must not fire across a prompt_version
    change — a prompt/code change landing between the failed attempt and the
    retry must force a full regeneration under the new prompt, not ship a
    body produced under the stale one."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch(
            "app.services.report_generator._render_full_md",
            side_effect=RuntimeError("render boom"),
        ),
        pytest.raises(RuntimeError, match="render boom"),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    row = db_session.execute(
        select(Report).where(Report.user_id == _USER, Report.report_date == _TODAY)
    ).scalar_one()
    assert row.status == "failed"
    assert row.report_inputs is not None
    assert row.report_inputs.get("pass2_raw")

    with (
        patch.object(rg, "_PROMPT_VERSION", rg._PROMPT_VERSION + "-bumped"),
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm) as mock_llm2,
    ):
        retried = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert retried.id == row.id
    assert retried.status == "success"
    mock_llm2.assert_called()  # full rerun, not the stage-skip reuse path
    assert retried.prompt_version == rg._PROMPT_VERSION + "-bumped"


def test_generate_report_retry_of_unsent_success_only_resends_email(
    db_session: Session,
) -> None:
    """#61: status=='success' but email_sent_at IS NULL (delivery never
    confirmed) must be resendable via a plain retry, without re-rendering or
    making any LLM call — the report body is already final."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch("app.services.report_generator.send_report_email", return_value=False) as mock_email,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.email_sent_at is None
    mock_email.assert_called_once()

    with (
        patch("app.services.report_generator.compute_portfolio") as mock_portfolio2,
        patch("app.services.report_generator.load_news_window") as mock_news2,
        patch("app.services.report_generator._openrouter_client") as mock_client2,
        patch("app.services.report_generator._call_llm") as mock_llm2,
        patch("app.services.report_generator._render_full_md") as mock_render2,
        patch("app.services.report_generator.send_report_email", return_value=True) as mock_email2,
    ):
        retried = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert retried.id == report.id
    assert retried.status == "success"
    mock_email2.assert_called_once()
    mock_llm2.assert_not_called()
    mock_client2.assert_not_called()
    mock_portfolio2.assert_not_called()
    mock_news2.assert_not_called()
    mock_render2.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: quiet-day skip
# ---------------------------------------------------------------------------


def test_generate_report_quiet_day_returns_skipped(db_session: Session) -> None:
    """No signals, no anomalies, no window news, no prior coverage → the only
    remaining reason to take the quiet shortcut (issue #440 narrowing) —
    status=skipped, no LLM call."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "skipped"
    mock_llm.assert_not_called()
    assert report.report_md is not None
    assert "§1 Portfolio Snapshot" in report.report_md


def test_generate_report_no_keyword_hit_but_news_present_runs_full_pipeline(
    db_session: Session,
) -> None:
    """issue #440 (owner decision 2026-09-12, "narrow/retire the quiet-path
    shortcut"): a zero-keyword-hit report period is no longer, by itself,
    grounds to take the canned quiet path — Requirements point 1 ("a zero-
    theme result... does not establish that the world has no macro
    developments"). With window news present (even though no macro keyword
    matched it) the full pipeline must run instead of short-circuiting."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Some unrelated headline")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm) as mock_llm,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    mock_llm.assert_called()


def test_generate_report_prior_success_coverage_alone_runs_full_pipeline(
    db_session: Session,
) -> None:
    """issue #440: eligible prior coverage (a continuing question this
    reader is owed an update on) is itself grounds to skip the quiet
    shortcut, even with zero keyword hits, zero anomalies, and zero window
    news this period."""
    from app.services import macro_coverage as mc

    earlier = Report(
        user_id=_USER,
        report_date=_TODAY - timedelta(days=2),
        report_type="incremental",
        session_node="manual",
        status="success",
        period_start=_NOW - timedelta(days=4),
        period_end=_NOW - timedelta(days=2),
    )
    db_session.add(earlier)
    db_session.flush()
    mc.persist_macro_coverage(
        db_session,
        report_id=earlier.id,
        user_id=_USER,
        as_of=earlier.period_end or _NOW,
        items=mc.extract_macro_sidecar(
            f"{mc._SIDECAR_START}\n"
            '{"items": [{"development_key": "fed-path", "coverage_mode": "NEW", '
            '"depth_tier": "anchor", "open_questions": ["will cuts continue?"]}]}'
            f"\n{mc._SIDECAR_END}"
        )[1],
    )
    db_session.commit()

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm) as mock_llm,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    mock_llm.assert_called()


def test_generate_report_quiet_day_unsent_email_does_not_log_sent(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """PR #181 review: send_report_email returning False now also means
    "recipient could not be resolved" (fail-closed), not just "commit
    failed after a real send". The caller's log line must not claim the
    email was sent when it demonstrably wasn't. Uses session_node=
    after_close so the short-manual-quiet suppression doesn't skip the
    email branch entirely."""
    import logging as _logging

    _logging.getLogger("app.services.report_generator").disabled = False
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm"),
        patch("app.services.report_generator.send_report_email", return_value=False),
        caplog.at_level("WARNING", logger="app.services.report_generator"),
    ):
        report = rg.generate_report(
            db_session, user_id=_USER, report_date=_TODAY, session_node="after_close"
        )

    assert report.status == "skipped"
    sent_claims = [r for r in caplog.records if "email sent" in r.getMessage().lower()]
    assert not sent_claims, (
        f"log line falsely claims delivery: {[r.getMessage() for r in sent_claims]}"
    )


# ---------------------------------------------------------------------------
# Tests: Tavily failure (degraded mode)
# ---------------------------------------------------------------------------


def test_generate_report_llm_failure_marks_failed(db_session: Session) -> None:
    """LLM exception → report persisted with status=failed, exception re-raised."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=RuntimeError("LLM down")),
        pytest.raises(RuntimeError, match="LLM down"),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    # The failed report should have been persisted
    from sqlalchemy import select

    from app.models.report import Report

    row = db_session.execute(
        select(Report).where(Report.user_id == _USER, Report.report_date == _TODAY)
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "failed"


def _mock_llm_noncompliant(
    client: object,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
    **kwargs: object,
) -> str:
    if with_holdings:
        return (
            "## §2 Macro Events\n\nYou should buy more semiconductors now. "
            "[For information only — not investment advice]\n\n"
            "## §3 Holdings Intelligence\n\nNVIDIA up 9%. [For information only — not investment advice]\n\n"
            "## §4 Exposure & Price Data\n\nConcentration watch. [For information only — not investment advice]\n\n"
            + _PASS2_FILLER
        )
    return _FAKE_LLM_PASS1


def test_generate_report_blocks_noncompliant_body(
    db_session: Session, _no_email: MagicMock
) -> None:
    """A body that trips the blacklist is held as needs_review and never emailed."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_noncompliant),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "needs_review"
    assert report.report_md is not None  # content preserved for inspection
    _no_email.assert_not_called()


def test_generate_report_retry_of_needs_review_unmarks_prior_surfaced_news(
    db_session: Session, _no_email: MagicMock
) -> None:
    """PR #139 review: generate_report reopens a needs_review row and reuses its
    frozen window — unmark_news_surfaced must run on the reopen so the retry's
    load_news_window call reselects the same candidate set the first attempt
    saw, instead of silently seeing a smaller set because of the first
    attempt's own marks."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_noncompliant),
        patch("app.services.report_generator.unmark_news_surfaced") as mock_unmark,
    ):
        report1 = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
        assert report1.status == "needs_review"
        mock_unmark.assert_not_called()  # fresh row — nothing to unmark yet

        report2 = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
        assert report2.id == report1.id  # same row, reopened for retry
        mock_unmark.assert_called_once_with(db_session, report1.id)


def test_generate_report_quiet_day_sends_heartbeat(
    db_session: Session, _no_email: MagicMock
) -> None:
    """A quiet week must still deliver a heartbeat email so silence != broken."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "skipped"
    _no_email.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: data window (#5), translation render (#8), re-render (#6)
# ---------------------------------------------------------------------------


def _normal_path_patches() -> list[object]:
    return [
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[_news_item("Fed")]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
    ]


def test_generate_report_includes_data_window(db_session: Session) -> None:
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    assert report.report_md is not None
    assert "Data window" in report.report_md


def test_generate_report_translates_when_output_lang_set(db_session: Session) -> None:
    """output_lang != en routes the assembled report through translation."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch(
                "app.services.report_generator._translate_md",
                side_effect=lambda md, lang: f"[{lang}]\n{md}",
            )
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY, output_lang="zh")
    assert report.report_md is not None
    assert "[zh]" in report.report_md
    # Issue #350 item 3: the footer follows output_lang too — zh-only here,
    # not the pre-#350 always-bilingual footer.
    assert "免责声明" in report.report_md
    assert "Disclaimer" not in report.report_md


def test_regenerate_render_is_token_free(db_session: Session) -> None:
    """mode=render rebuilds from stored Pass 2 body with no LLM call."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    with (
        patch(
            "app.services.report_generator._call_llm",
            side_effect=AssertionError("render must not call the LLM"),
        ),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("render must not re-fetch"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="render", output_lang="en")

    assert out.status == "success"
    assert out.report_md is not None
    assert "§1 Portfolio Snapshot" in out.report_md
    assert "Data window" in out.report_md


def test_regenerate_analyze_reruns_pass2_from_stored_intel(db_session: Session) -> None:
    """mode=analyze re-runs Pass 2 only — no news/Tavily/Pass 1 re-fetch."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    new_body = (
        "## §2 Macro Events\n\nReanalyzed view. [For information only — not investment advice]\n\n"
        "## §3 Holdings Intelligence\n\nNVIDIA up 9%. [For information only — not investment advice]\n\n"
        "## §4 Exposure & Price Data\n\nConcentration watch. [For information only — not investment advice]\n\n"
        + _PASS2_FILLER
    )
    with (
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", return_value=new_body),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("analyze must not re-fetch news"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="analyze", output_lang="en")

    assert out.report_md is not None
    assert "Reanalyzed view" in out.report_md
    assert out.report_inputs is not None
    assert "Reanalyzed view" in out.report_inputs["pass2_raw"]


def test_regenerate_analyze_replaces_macro_coverage_dropping_removed_topics(
    db_session: Session,
) -> None:
    """issue #440 Contract constraints "Regenerate after a topic was
    removed: No stale coverage for that report" — analyze mode's freshly
    extracted sidecar must REPLACE this report's coverage rows, not upsert
    on top of them, and the sidecar itself must never reach report_md."""
    from sqlalchemy import select as sa_select

    from app.models.macro_coverage import MacroCoverage
    from app.services import macro_coverage as mc

    first_sidecar = (
        f"{mc._SIDECAR_START}\n"
        '{"items": [{"development_key": "topic-a", "coverage_mode": "NEW", '
        '"depth_tier": "anchor"}, {"development_key": "topic-b", "coverage_mode": "NEW", '
        '"depth_tier": "context"}]}'
        f"\n{mc._SIDECAR_END}"
    )

    def _mock_llm_first(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kwargs: object,
    ) -> str:
        if with_holdings:
            return _FAKE_LLM_PASS2 + first_sidecar
        return _FAKE_LLM_PASS1

    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator._call_llm", side_effect=_mock_llm_first)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    rows = (
        db_session.execute(sa_select(MacroCoverage).where(MacroCoverage.report_id == rid))
        .scalars()
        .all()
    )
    assert {r.development_key for r in rows} == {"topic-a", "topic-b"}

    second_sidecar = (
        f"{mc._SIDECAR_START}\n"
        '{"items": [{"development_key": "topic-a", "coverage_mode": "UPDATE", '
        '"depth_tier": "anchor"}]}'
        f"\n{mc._SIDECAR_END}"
    )
    new_body = (
        "## §2 Macro Events\n\nReanalyzed view. [For information only — not investment advice]\n\n"
        "## §3 Holdings Intelligence\n\nNVIDIA up 9%. [For information only — not investment advice]\n\n"
        "## §4 Exposure & Price Data\n\nConcentration watch. [For information only — not investment advice]\n\n"
        + _PASS2_FILLER
        + second_sidecar
    )
    with (
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", return_value=new_body),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("analyze must not re-fetch news"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="analyze", output_lang="en")

    assert out.report_md is not None
    assert "MACRO_COVERAGE" not in out.report_md
    rows = (
        db_session.execute(sa_select(MacroCoverage).where(MacroCoverage.report_id == rid))
        .scalars()
        .all()
    )
    assert {r.development_key for r in rows} == {"topic-a"}
    assert next(r for r in rows if r.development_key == "topic-a").coverage_mode == "UPDATE"


def test_regenerate_render_does_not_touch_macro_coverage(db_session: Session) -> None:
    """Design §5: "Render regeneration preserves its existing non-fetching/
    non-analysis behavior" — mode=render must not re-derive or replace this
    report's coverage rows, and must still strip any sidecar present in the
    stored body from the re-rendered output."""
    from sqlalchemy import select as sa_select

    from app.models.macro_coverage import MacroCoverage
    from app.services import macro_coverage as mc

    sidecar = (
        f"{mc._SIDECAR_START}\n"
        '{"items": [{"development_key": "topic-a", "coverage_mode": "NEW", '
        '"depth_tier": "anchor"}]}'
        f"\n{mc._SIDECAR_END}"
    )

    def _mock_llm_with_sidecar(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kwargs: object,
    ) -> str:
        if with_holdings:
            return _FAKE_LLM_PASS2 + sidecar
        return _FAKE_LLM_PASS1

    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator._call_llm", side_effect=_mock_llm_with_sidecar)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="render", output_lang="en")
    assert out.report_md is not None
    assert "MACRO_COVERAGE" not in out.report_md
    rows = (
        db_session.execute(sa_select(MacroCoverage).where(MacroCoverage.report_id == rid))
        .scalars()
        .all()
    )
    assert {r.development_key for r in rows} == {"topic-a"}


# --- R-7: short manual quiet-window email suppression ----------------------


def test_is_short_manual_quiet_true_for_tiny_empty_manual_window() -> None:
    start = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    end = datetime(2026, 6, 10, 12, 18, tzinfo=UTC)
    assert rg._is_short_manual_quiet("manual", start, end, [], []) is True


def test_is_short_manual_quiet_false_for_scheduled_trigger() -> None:
    start = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    end = datetime(2026, 6, 10, 12, 18, tzinfo=UTC)
    assert rg._is_short_manual_quiet("after_close", start, end, [], []) is False


def test_is_short_manual_quiet_false_when_window_long() -> None:
    start = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    end = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    assert rg._is_short_manual_quiet("manual", start, end, [], []) is False


def test_is_short_manual_quiet_false_when_news_present() -> None:
    start = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    end = datetime(2026, 6, 10, 12, 18, tzinfo=UTC)
    assert rg._is_short_manual_quiet("manual", start, end, [_news_item("x")], []) is False


# ---------------------------------------------------------------------------
# Tests: integration — inline markers are stripped from the generated report (#9)
# ---------------------------------------------------------------------------


def _mock_llm_f2(
    client: object,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
    **kwargs: object,
) -> str:
    if with_holdings:
        return _FAKE_LLM_PASS2_F2
    return _FAKE_LLM_PASS1


def test_generate_report_strips_inline_markers(db_session: Session) -> None:
    """The generated report must carry no [S#] citations, provenance tags, or
    per-sentence disclaimer suffixes — only the footer disclaimer remains (#9)."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm_f2),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_md is not None
    md = report.report_md
    # Body markers are gone.
    assert "[S1]" not in md
    assert "[新闻]" not in md
    assert "[行情]" not in md
    assert "[分析]" not in md
    # The body's per-sentence disclaimer suffix is stripped, but the footer's
    # single bilingual disclaimer (which legitimately says "not investment advice")
    # remains — so the phrase still appears, only in the footer.
    assert "Notes & Disclaimer" in md


# ---------------------------------------------------------------------------
# Tests: F3 footer (integration — the code-built footer itself is covered in
# test_report_sections.py)
# ---------------------------------------------------------------------------


def test_generate_report_normal_path_has_footer(db_session: Session) -> None:
    """Footer must appear in every successfully generated report."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_md is not None
    # Issue #350 item 3: the footer renders in output_lang ONLY — this test
    # calls generate_report with no output_lang (defaults to "en"), so the
    # footer is English-only, not the pre-#350 always-bilingual footer.
    assert "Notes & Disclaimer" in report.report_md
    assert "免责声明" not in report.report_md


def test_generate_report_quiet_day_has_footer(db_session: Session) -> None:
    """Footer must also appear on quiet-day (status=skipped) reports."""
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "skipped"
    mock_llm.assert_not_called()
    assert report.report_md is not None
    assert "Notes & Disclaimer" in report.report_md
    assert "免责声明" not in report.report_md


# ---------------------------------------------------------------------------
# Tests: explicit user_id (issue #128 A1)
# ---------------------------------------------------------------------------

_OTHER_USER = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def test_generate_report_uses_explicit_user_id(db_session: Session) -> None:
    """user_id (issue #129 B3: required, no ambient fallback) is used as-is
    for the report row — the multi-user fan-out (report_tasks.py) relies on
    this to generate each user's report under their own identity."""
    seed_user(db_session, _OTHER_USER)
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 0)),
        patch("app.services.report_generator._call_llm") as mock_llm,
    ):
        report = rg.generate_report(db_session, user_id=_OTHER_USER, report_date=_TODAY)

    mock_llm.assert_not_called()  # quiet day, never reaches Pass 1/2
    assert report.user_id == _OTHER_USER


def test_generate_report_forwards_moves_cache_to_detect_window_anomalies(
    db_session: Session,
) -> None:
    """moves_cache must reach detect_window_anomalies unchanged — this is
    the plumbing that lets report_tasks.py's fan-out share one
    compute_global_moves() call across a whole batch."""
    cache: MovesCache = {}
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_quiet_signals()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([], 0)
        ) as mock_detect,
        patch("app.services.report_generator._call_llm"),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY, moves_cache=cache)

    assert mock_detect.call_args.args[-1] is cache


def test_regenerate_analyze_reuses_stored_base_currency_by_default(
    db_session: Session,
) -> None:
    """Issue #350 item 1: `base_currency=None` (the default) preserves the
    pre-#350 behavior of re-using the ORIGINAL report's stored
    portfolio_summary.base_currency for the live refetch — an existing
    caller that never passes this parameter stays byte-for-byte unchanged.

    `generate_report` itself runs under `_normal_path_patches`'s
    `compute_portfolio` mock, which always returns `_portfolio_snap()`
    (base_currency="USD") regardless of the `base_currency` argument — so
    the ORIGINAL report's stored value is set directly here, rather than
    relying on that mock to respect an argument it deliberately ignores.
    """
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_inputs is not None
    stored_inputs = dict(report.report_inputs)
    stored_inputs["portfolio_summary"] = {
        **stored_inputs["portfolio_summary"],
        "base_currency": "CNY",
    }
    report.report_inputs = stored_inputs
    db_session.commit()

    with (
        patch(
            "app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()
        ) as mock_compute,
        patch("app.services.report_generator._call_llm", return_value=_FAKE_LLM_PASS2),
    ):
        rg.regenerate_report(db_session, report.id, user_id=_USER, mode="analyze")

    assert mock_compute.call_args.kwargs["base_currency"] == "CNY"


def test_regenerate_analyze_base_currency_override(db_session: Session) -> None:
    """An explicit `base_currency` override wins over the report's stored
    value — this is what routers/reports.py's regenerate endpoint and
    routers/admin.py's rerun endpoint use to apply the caller's CURRENT
    users.base_currency preference instead of the stale one baked into the
    original report."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    # Stored value is "USD" (the mocked compute_portfolio's own snapshot) —
    # an explicit override below must still win over it.
    assert report.report_inputs is not None
    assert report.report_inputs["portfolio_summary"]["base_currency"] == "USD"

    with (
        patch(
            "app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()
        ) as mock_compute,
        patch("app.services.report_generator._call_llm", return_value=_FAKE_LLM_PASS2),
    ):
        rg.regenerate_report(
            db_session, report.id, user_id=_USER, mode="analyze", base_currency="CNY"
        )

    assert mock_compute.call_args.kwargs["base_currency"] == "CNY"


_REMOVED_INPUT_KEYS: dict[str, Any] = {
    "ticker_intel": {"NVDA": "old L1 brief"},
    "macro_event_intel": {"theme:rates": {"analysis": "old L2", "affected_asset_classes": []}},
    "macro_event_exposure": {"theme:rates": ["STOCK"]},
    "cross_name_intel": [{"identifiers": ["NVDA", "AAPL"], "summary": "old L3"}],
    "body_source": "pass2",
    "assembly_model": "",
    "assembly_prompt": "",
    "assembly_raw": "",
    "assembly_prompt_version": "",
    "assembly_shadow": {},
    "search_queries": ["old query"],
}


def test_issue_640_regenerate_ignores_removed_report_input_keys(db_session: Session) -> None:
    """Issue #640 acceptance 3: a stored row that still carries keys from the
    removed L1/L2/L3, assembly and report-time search paths regenerates in
    both modes."""
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    report.report_inputs = {**dict(report.report_inputs or {}), **_REMOVED_INPUT_KEYS}
    db_session.commit()

    with patch("app.services.report_generator._call_llm") as mock_llm:
        rendered = rg.regenerate_report(db_session, report.id, user_id=_USER, mode="render")
    mock_llm.assert_not_called()
    assert rendered.report_md is not None
    assert "NVIDIA up 9%" in rendered.report_md

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator._call_llm", return_value=_FAKE_LLM_PASS2) as llm,
    ):
        analyzed = rg.regenerate_report(db_session, report.id, user_id=_USER, mode="analyze")
    assert llm.call_count == 1
    assert analyzed.report_inputs is not None
    assert analyzed.report_inputs["pass2_raw"] == _FAKE_LLM_PASS2
    assert "old L1 brief" not in analyzed.report_inputs["pass2_prompt"]


def test_issue_640_report_context_ignores_removed_keys() -> None:
    """The #61 resume path rehydrates old rows through `from_jsonb`."""
    from app.services.report_context import ReportContext

    ctx = ReportContext.from_jsonb({"pass2_raw": "body", **_REMOVED_INPUT_KEYS})
    assert ctx.pass2_raw == "body"
    assert "ticker_intel" not in ctx.to_jsonb()


# ---------------------------------------------------------------------------
# B6 investor preferences (issue #129 checkpoint B6, decision point 6)
# ---------------------------------------------------------------------------


def _seed_investment_context(
    session: Session, user_id: uuid.UUID, *, locale: str = "zh", intel_focus: str = "GEOPOLITICS"
) -> None:
    from app.models.user import User
    from app.models.user_investment_context import UserInvestmentContext

    # The module-level `_seed_test_user` autouse fixture already creates a
    # `users` row for _USER (issue #129 B7); update it in place rather than
    # inserting a second row under the same primary key.
    existing = session.get(User, user_id)
    if existing is not None:
        existing.locale = locale
    else:
        session.add(
            User(
                id=user_id,
                auth_provider="supabase",
                auth_subject=f"sub-{user_id}",
                email=f"{user_id}@example.com",
                status="active",
                locale=locale,
                base_currency="USD",
                report_cadence="mwf",
            )
        )
    session.add(
        UserInvestmentContext(
            user_id=user_id,
            questionnaire={
                "asset_scale": "500K_2M",
                "markets": ["US"],
                "style": "GROWTH",
                "horizon": "LONG",
                "risk_appetite": "AGGRESSIVE",
                "sectors_of_interest": ["Technology"],
                "objective": "GROWTH",
                "intel_focus": intel_focus,
            },
            questionnaire_version="v1",
            free_text="Concentrated in AI infrastructure names on purpose.",
        )
    )
    session.flush()


def test_generate_report_pass2_prompt_carries_locale_and_intel_focus(
    db_session: Session,
) -> None:
    _seed_investment_context(db_session, _USER, locale="zh", intel_focus="GEOPOLITICS")
    captured: dict[str, str] = {}

    def _capture_pass2_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kw: object,
    ) -> str:
        if with_holdings:
            captured["pass2_user"] = user
            return _FAKE_LLM_PASS2
        return _FAKE_LLM_PASS1

    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator._call_llm", side_effect=_capture_pass2_llm)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert "INVESTOR PREFERENCES" in captured["pass2_user"]
    assert "Reader locale: zh" in captured["pass2_user"]
    assert "geopolitical developments" in captured["pass2_user"]


def test_generate_report_snapshots_questionnaire_into_report_inputs(db_session: Session) -> None:
    """§8.4/§8.6: the closed-enum answers actually used for this report are
    snapshotted for audit. free_text is NOT folded into that dedicated
    snapshot dict (see investment_context.py's InvestorPreferences
    docstring for the precise, narrower guarantee this is — free_text
    inevitably still appears inside the stored pass2_prompt text itself,
    same as holdings data already does; what this guards against is
    free_text ALSO existing as its own plainly-labeled, bulk-queryable key)."""
    _seed_investment_context(db_session, _USER, locale="zh", intel_focus="GEOPOLITICS")
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.report_inputs is not None
    snap = report.report_inputs["investor_questionnaire_snapshot"]
    assert snap["intel_focus"] == "GEOPOLITICS"
    assert snap["risk_appetite"] == "AGGRESSIVE"
    assert report.report_inputs["investor_questionnaire_version"] == "v1"
    assert "free_text" not in snap


def test_generate_report_with_no_questionnaire_omits_intel_focus(db_session: Session) -> None:
    """§8.6 'can be skipped': no UserInvestmentContext row -> no intel_focus
    in the prompt, but locale (from users.locale, NOT NULL) still renders
    once a users row exists; with no users row either, locale falls back to
    'en' inside load_investor_preferences and the block is fully omitted
    only when both are absent — this exercises the no-row-at-all case."""
    captured: dict[str, str] = {}

    def _capture_pass2_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kw: object,
    ) -> str:
        if with_holdings:
            captured["pass2_user"] = user
            return _FAKE_LLM_PASS2
        return _FAKE_LLM_PASS1

    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(
            patch("app.services.report_generator._call_llm", side_effect=_capture_pass2_llm)
        )
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert "Stated intel focus" not in captured["pass2_user"]


def test_regenerate_render_does_not_refetch_investor_preferences(db_session: Session) -> None:
    _seed_investment_context(db_session, _USER, locale="zh", intel_focus="GEOPOLITICS")
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    with (
        patch(
            "app.services.report_generator.load_investor_preferences",
            side_effect=AssertionError("render must not re-fetch investor preferences"),
        ),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("render must not re-fetch"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="render", output_lang="en")
    assert out.report_md is not None


def test_regenerate_analyze_refreshes_investor_preferences(db_session: Session) -> None:
    """A user who changed their questionnaire answers between the original
    generation and this regenerate should see the NEW answers reflected —
    same "re-fetched live" treatment as fresh_technical/fresh_exposure."""
    _seed_investment_context(db_session, _USER, locale="zh", intel_focus="MACRO")
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    rid = report.id

    # Re-answer with a different intel_focus.
    from app.models.user_investment_context import UserInvestmentContext

    ctx = db_session.get(UserInvestmentContext, _USER)
    assert ctx is not None
    ctx.questionnaire = {**ctx.questionnaire, "intel_focus": "FUNDAMENTALS"}
    db_session.flush()

    captured: dict[str, str] = {}

    def _capture_pass2_llm(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kw: object,
    ) -> str:
        captured["pass2_user"] = user
        return _FAKE_LLM_PASS2

    with (
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_capture_pass2_llm),
        patch(
            "app.services.report_generator.load_news_window",
            side_effect=AssertionError("analyze must not re-fetch news"),
        ),
    ):
        out = rg.regenerate_report(db_session, rid, user_id=_USER, mode="analyze", output_lang="en")

    assert "individual-holding fundamentals" in captured["pass2_user"]
    assert out.report_inputs is not None
    assert out.report_inputs["investor_questionnaire_snapshot"]["intel_focus"] == "FUNDAMENTALS"


# ---------------------------------------------------------------------------
# Tests: issue #173 — §3 proportionality check wiring (log-only)
# ---------------------------------------------------------------------------

_S3_CHECK_PORTFOLIO: dict[str, Any] = {
    "holdings": [
        {"ticker": "AAPL", "name": "Apple Inc.", "market_value_base": 900.0, "position": 0}
    ],
    "total_base": 1000.0,
}
_S3_CHECK_RAW_BODY = (
    "## §2 Macro Events\n\nNothing notable this period.\n\n"
    "## §3 Holdings Context\n\nAAPL had a quiet day.\n\n"
    "## §4 Exposure & Price Data\n\nNothing notable."
)


def test_render_full_md_section3_check_is_log_only() -> None:
    """Contract constraints acceptance test 5: report content is
    byte-for-byte unaffected by whether the check logs a mismatch."""
    with_check = rg._render_full_md(
        "2026-09-10",
        _S3_CHECK_PORTFOLIO,
        [],
        _S3_CHECK_RAW_BODY,
        "en",
        report_id=uuid.uuid4(),
        holding_news={},
    )
    without_check = rg._render_full_md(
        "2026-09-10",
        _S3_CHECK_PORTFOLIO,
        [],
        _S3_CHECK_RAW_BODY,
        "en",
    )
    assert with_check == without_check


def test_render_full_md_wires_section3_check_and_logs_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AAPL is 90% of the portfolio but §3 gives it one short sentence with
    no structural evidence — this must surface as a logged mismatch once
    `_render_full_md` is given report_id + holding_news (issue #173
    Requirements item 3: the check runs from real report-generation
    wiring, not only as a standalone module)."""
    # docs/playbooks/testing-notes.md: alembic's session migrate disables
    # already-imported module loggers, so caplog would see nothing.
    logging.getLogger("app.services.section3_proportionality").disabled = False
    with caplog.at_level(logging.WARNING, logger="app.services.section3_proportionality"):
        rg._render_full_md(
            "2026-09-10",
            _S3_CHECK_PORTFOLIO,
            [],
            _S3_CHECK_RAW_BODY,
            "en",
            report_id=uuid.uuid4(),
            holding_news={},
        )
    assert "AAPL" in caplog.text
    assert "proportionality mismatch" in caplog.text


def test_render_full_md_without_report_id_skips_section3_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The quiet-day render path passes neither report_id nor holding_news
    — confirms that omission opts out of the check entirely rather than
    erroring."""
    with caplog.at_level(logging.WARNING, logger="app.services.section3_proportionality"):
        rg._render_full_md(
            "2026-09-10",
            _S3_CHECK_PORTFOLIO,
            [],
            _S3_CHECK_RAW_BODY,
            "en",
        )
    assert caplog.text == ""


def test_render_full_md_section3_check_failure_does_not_break_render(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The check must never take the report render down with it — a bug in
    the check itself is caught and logged, not propagated."""

    def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(rg, "check_section3_proportionality", _boom)
    # docs/playbooks/testing-notes.md: alembic's session migrate disables
    # already-imported module loggers, so caplog would see nothing.
    logging.getLogger("app.services.report_generator").disabled = False
    with caplog.at_level(logging.ERROR, logger="app.services.report_generator"):
        full_md, _violations, _translated_body = rg._render_full_md(
            "2026-09-10",
            _S3_CHECK_PORTFOLIO,
            [],
            _S3_CHECK_RAW_BODY,
            "en",
            report_id=uuid.uuid4(),
            holding_news={},
        )
    assert full_md  # rendering still completed despite the check raising
    assert "§3 proportionality check raised" in caplog.text


# ---------------------------------------------------------------------------
# Tests: PR #423 review (blacktomb42) — holding display `name` must be a
# matchable §3 identifier, not just ticker + configured entity_aliases.
# ---------------------------------------------------------------------------


def test_build_holding_check_inputs_includes_holding_display_name_in_alias_terms() -> None:
    """AAPL has no `entity_aliases` row in holding_news_keywords.yml, and §3
    routinely says "Apple" rather than repeating the ticker — alias_terms
    must include the holding's own `name` field so that prose still
    attributes correctly."""
    portfolio: dict[str, Any] = {
        "holdings": [
            {"ticker": "AAPL", "name": "Apple Inc.", "market_value_base": 900.0, "position": 0}
        ],
        "total_base": 1000.0,
    }
    check_inputs = rg._build_holding_check_inputs(portfolio, holding_news={}, anomalies=[])
    assert len(check_inputs) == 1
    holding = check_inputs[0]
    assert "AAPL" in holding.alias_terms
    assert "Apple Inc." in holding.alias_terms


def test_prose_naming_holding_by_display_name_gets_nonzero_attributed_length() -> None:
    """End-to-end (via the real alias_terms `_build_holding_check_inputs`
    assembles): §3 prose naming a holding by its English company name, or
    by a Chinese name with no ticker mentioned at all, must attribute
    nonzero paragraph length — not silently fall to `actual_len=0` because
    only the ticker/fund_code was ever matchable."""
    portfolio: dict[str, Any] = {
        "holdings": [
            {"ticker": "AAPL", "name": "Apple Inc.", "market_value_base": 100.0, "position": 0},
            {"fund_code": "00700", "name": "腾讯控股", "market_value_base": 50.0, "position": 1},
        ],
        "total_base": 1000.0,
    }
    check_inputs = rg._build_holding_check_inputs(portfolio, holding_news={}, anomalies=[])
    identifier_terms = {c.identifier: c.alias_terms for c in check_inputs}
    apple_ident = next(c.identifier for c in check_inputs if "AAPL" in c.alias_terms)
    tencent_ident = next(c.identifier for c in check_inputs if c.identifier != apple_ident)

    section3_body = (
        "Apple's supplier base grew this quarter, a filed contract confirms the relationship.\n\n"
        "腾讯控股本季度与主要合作伙伴续签协议，客户基础进一步扩大。"  # noqa: RUF001
    )
    segments = s3p.segment_section3_by_holding(section3_body, identifier_terms)
    assert len(segments.by_identifier[apple_ident]) > 0
    assert len(segments.by_identifier[tencent_ident]) > 0


# ---------------------------------------------------------------------------
# Tests: issue #421 — watch_tier applies a config-driven FLOOR (not an
# absolute substitute) to real position weight in _build_holding_check_inputs
# (Design item 7, amended per PR #425 review blacktomb42 blocker 1: a large
# real-weight holding tagged watched must never have its §3 depth SHRUNK by
# a smaller config tier weight — max(real, config), never a replacement).
# ---------------------------------------------------------------------------


def _watched_portfolio(watch_tier: str | None, market_value_base: float = 0.0) -> dict[str, Any]:
    return {
        "holdings": [
            {
                "ticker": "TRACK",
                "name": "Tracked Startup",
                "market_value_base": market_value_base,
                "position": 0,
                "watch_tier": watch_tier,
            }
        ],
        "total_base": 1000.0,
    }


def test_watch_tier_critical_floors_zero_weight_to_configured_weight() -> None:
    """Contract constraints acceptance test 2: a watch_tier="critical"
    holding with real weight 0 (market_value_base=0) must receive
    weight=0.15 from watch_tier_weights.yml, not its real position weight —
    the floor lifts it since 0.15 > 0 real."""
    check_inputs = rg._build_holding_check_inputs(
        _watched_portfolio("critical"), holding_news={}, anomalies=[]
    )
    assert len(check_inputs) == 1
    assert check_inputs[0].weight == pytest.approx(0.15)


@pytest.mark.parametrize("tier,expected", [("watch", 0.05), ("focus", 0.10), ("critical", 0.15)])
def test_watch_tier_floor_comes_from_config_for_every_tier(tier: str, expected: float) -> None:
    """Contract constraints acceptance test 3: watch/focus/critical each
    read their floor weight from watch_tier_weights.yml, not a hardcoded
    per-call value — real weight is 0 here, so the floor is what wins."""
    check_inputs = rg._build_holding_check_inputs(
        _watched_portfolio(tier), holding_news={}, anomalies=[]
    )
    assert check_inputs[0].weight == pytest.approx(expected)


def test_watch_tier_never_shrinks_a_larger_real_weight() -> None:
    """PR #425 review blocker 1: a 40% real-weight holding tagged
    watch_tier="critical" must be checked at 0.40, NOT shrunk to the
    config's 0.15 — tagging a large real holding as watched must never
    reduce its expected §3 depth versus leaving the tier null."""
    portfolio = _watched_portfolio("critical", market_value_base=400.0)  # 400/1000 = 0.40
    check_inputs = rg._build_holding_check_inputs(portfolio, holding_news={}, anomalies=[])
    assert check_inputs[0].weight == pytest.approx(0.40)


def test_watch_tier_floor_lifts_a_smaller_real_weight() -> None:
    """A small-but-nonzero real weight (0.02) tagged watch_tier="watch"
    still gets lifted to the 0.05 floor — the floor applies whenever real
    weight is below the tier's configured weight, not only at exactly 0."""
    portfolio = _watched_portfolio("watch", market_value_base=20.0)  # 20/1000 = 0.02
    check_inputs = rg._build_holding_check_inputs(portfolio, holding_news={}, anomalies=[])
    assert check_inputs[0].weight == pytest.approx(0.05)


def test_unwatched_holding_still_uses_real_weight() -> None:
    """watch_tier absent/None (the pre-#421, still-default case) must be
    completely unaffected — real position weight, unchanged from #173."""
    portfolio = _watched_portfolio(None, market_value_base=250.0)
    check_inputs = rg._build_holding_check_inputs(portfolio, holding_news={}, anomalies=[])
    assert check_inputs[0].weight == pytest.approx(0.25)  # 250 / 1000


def test_watch_tier_cleared_to_null_reverts_to_real_zero_weight() -> None:
    """Contract constraints acceptance test 4: PATCH watch_tier=null must
    make the §3 call use the holding's real (zero) position weight again,
    matching unwatched-holding behavior — not silently keep a stale
    config-driven weight or drop the holding."""
    portfolio = _watched_portfolio(None, market_value_base=0.0)
    check_inputs = rg._build_holding_check_inputs(portfolio, holding_news={}, anomalies=[])
    assert len(check_inputs) == 1
    assert check_inputs[0].weight == 0.0


# ---------------------------------------------------------------------------
# Tests: PR #425 review soft note 2 — broken/unreadable
# watch_tier_weights.yml must send a daily-deduped ops alert, not just skip
# quietly. Report generation still completes (fail-soft for the user-facing
# report); the floor is skipped (falls back to real weight) for this run.
# ---------------------------------------------------------------------------


@pytest.fixture
def _production_env() -> Generator[None, None, None]:
    """Mirrors fx_fetcher.py's test fixture of the same shape (issue #354):
    ops alerts are gated on APP_ENV=="production" so local/test runs never
    send real alerts for an expected-broken-in-dev config."""
    get_settings.cache_clear()
    with patch.dict("os.environ", {"APP_ENV": "production"}):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def test_broken_watch_tier_config_falls_back_to_real_weight(tmp_path: Any) -> None:
    """A broken config must not crash report generation or drop the
    holding — every watched holding's §3 call falls back to real weight
    for this run, same as if watch_tier were unset."""
    broken = tmp_path / "watch_tier_weights.yml"
    broken.write_text("watch: 0.05\n", encoding="utf-8")  # missing focus/critical
    with patch.object(get_settings(), "WATCH_TIER_WEIGHTS_CONFIG_PATH", str(broken)):
        portfolio = _watched_portfolio("critical", market_value_base=0.0)
        check_inputs = rg._build_holding_check_inputs(portfolio, holding_news={}, anomalies=[])
    assert len(check_inputs) == 1
    assert check_inputs[0].weight == 0.0  # real weight, floor skipped


def test_broken_watch_tier_config_sends_ops_alert(tmp_path: Any, _production_env: None) -> None:
    broken = tmp_path / "watch_tier_weights.yml"
    broken.write_text("watch: 0.05\n", encoding="utf-8")
    with (
        patch.object(get_settings(), "WATCH_TIER_WEIGHTS_CONFIG_PATH", str(broken)),
        patch.object(rg, "send_ops_alert", return_value=True) as mock_alert,
    ):
        rg._build_holding_check_inputs(
            _watched_portfolio("critical"), holding_news={}, anomalies=[]
        )
    assert mock_alert.call_count == 1
    assert "watch_tier_weights" in mock_alert.call_args.kwargs["subject"]
    assert mock_alert.call_args.kwargs["severity"] == "ALERT"


def test_broken_watch_tier_config_alert_deduped_same_day(
    tmp_path: Any, _production_env: None
) -> None:
    broken = tmp_path / "watch_tier_weights.yml"
    broken.write_text("watch: 0.05\n", encoding="utf-8")
    with (
        patch.object(get_settings(), "WATCH_TIER_WEIGHTS_CONFIG_PATH", str(broken)),
        patch.object(rg, "send_ops_alert", return_value=True) as mock_alert,
    ):
        rg._build_holding_check_inputs(
            _watched_portfolio("critical"), holding_news={}, anomalies=[]
        )
        rg._build_holding_check_inputs(_watched_portfolio("focus"), holding_news={}, anomalies=[])
    assert mock_alert.call_count == 1


def test_no_watch_tier_config_alert_outside_production(tmp_path: Any) -> None:
    broken = tmp_path / "watch_tier_weights.yml"
    broken.write_text("watch: 0.05\n", encoding="utf-8")
    with (
        patch.object(get_settings(), "WATCH_TIER_WEIGHTS_CONFIG_PATH", str(broken)),
        patch.object(rg, "send_ops_alert", return_value=True) as mock_alert,
    ):
        rg._build_holding_check_inputs(
            _watched_portfolio("critical"), holding_news={}, anomalies=[]
        )
    assert mock_alert.call_count == 0


def test_valid_watch_tier_config_never_alerts(_production_env: None) -> None:
    with patch.object(rg, "send_ops_alert", return_value=True) as mock_alert:
        rg._build_holding_check_inputs(
            _watched_portfolio("critical"), holding_news={}, anomalies=[]
        )
    assert mock_alert.call_count == 0


def _generate_report_with_snap(db_session: Session, snap: PortfolioSnapshot) -> MagicMock:
    """Run generate_report far enough to fire the post-snapshot ops alerts."""
    mock_alert = MagicMock()
    with (
        patch("app.services.report_generator.compute_portfolio", return_value=snap),
        patch(
            "app.services.report_generator.load_news_window",
            return_value=[_news_item("Fed raises rates")],
        ),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch(
            "app.services.report_generator.detect_window_anomalies", return_value=([_anomaly()], 2)
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_llm),
        patch.object(rg, "send_ops_alert", mock_alert),
    ):
        rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)
    return mock_alert


def test_price_missing_alert_is_warning(db_session: Session) -> None:
    snap = _portfolio_snap()
    snap.stale_tickers = ["MISSING"]
    mock_alert = _generate_report_with_snap(db_session, snap)
    missing = next(c for c in mock_alert.call_args_list if "price missing" in c.kwargs["subject"])
    assert missing.kwargs["severity"] == "WARNING"


def test_price_stale_alert_is_warning(db_session: Session) -> None:
    snap = _portfolio_snap()
    snap.stale_priced_tickers = ["AAPL"]
    mock_alert = _generate_report_with_snap(db_session, snap)
    stale = next(c for c in mock_alert.call_args_list if "price data stale" in c.kwargs["subject"])
    assert stale.kwargs["severity"] == "WARNING"


def test_report_fx_stale_alert_is_warning(db_session: Session) -> None:
    snap = _portfolio_snap()
    snap.fx_rates_as_of = {"CNY": date(2026, 5, 20)}
    mock_alert = _generate_report_with_snap(db_session, snap)
    fx_stale = next(c for c in mock_alert.call_args_list if "FX rates stale" in c.kwargs["subject"])
    assert fx_stale.kwargs["severity"] == "WARNING"


# ---------------------------------------------------------------------------
# Issue #560 — §1 closing page-links line is template-layer copy, spliced in
# after translation so the LLM never sees or paraphrases it.
# ---------------------------------------------------------------------------

_LINKS_PORTFOLIO: dict[str, Any] = {
    "base_currency": "USD",
    "total_base": 10000,
    "fx_rates_as_of": {},
    "holdings": [],
    "by_market": {"US": 10000},
}
_LINKS_RAW_BODY = "## §2 Macro Context\n\nRates were steady.\n"


def test_render_full_md_places_page_links_at_end_of_section1_en() -> None:
    from app.services.report_sections import _build_section1_page_links

    full_md, _v, _t = rg._render_full_md("2026-09-25", _LINKS_PORTFOLIO, [], _LINKS_RAW_BODY, "en")
    links = _build_section1_page_links("en")
    assert links in full_md
    assert full_md.index("**Distribution:**") < full_md.index(links) < full_md.index("## §2")


def test_render_full_md_zh_keeps_page_links_out_of_translation() -> None:
    from app.services.report_sections import _build_section1_page_links

    seen: list[str] = []

    def _fake_translate(md: str, lang: str) -> str:
        seen.append(md)
        return f"[{lang}]\n{md}"

    with patch("app.services.report_generator._translate_md", side_effect=_fake_translate):
        full_md, violations, _t = rg._render_full_md(
            "2026-09-25", _LINKS_PORTFOLIO, [], _LINKS_RAW_BODY, "zh"
        )
    zh_links = _build_section1_page_links("zh")
    assert zh_links in full_md
    assert full_md.index(zh_links) < full_md.index("## §2")
    assert all("Want a closer look" not in md and zh_links not in md for md in seen)
    assert violations == []


# Issue #583: Traditional rendering happens only after both compliance scans.
@pytest.mark.parametrize("body", ["市场风险值得关注。", "市场风险:强烈买入。"])
def test_render_zh_hant_scans_before_conversion(body: str) -> None:
    from app.services.zh_hant import to_traditional

    events: list[str] = []
    from app.compliance.output_scan import _scan_forbidden_output
    from app.services.report_sections import _build_footer

    real_scan = _scan_forbidden_output

    def scan(text: str) -> list[str]:
        events.append("scan")
        assert "市场风险" in text
        assert "市場風險" not in text
        return real_scan(text)

    def convert(text: str) -> str:
        events.append("convert")
        assert events[:2] == ["scan", "scan"]
        return to_traditional(text)

    with (
        patch.object(rg, "_translate_md", return_value=body) as translate,
        patch.object(rg, "_scan_forbidden_output", side_effect=scan),
        patch.object(rg, "to_traditional", side_effect=convert, create=True),
    ):
        full_md, violations, dynamic = rg._render_full_md(
            "2026-09-29", _LINKS_PORTFOLIO, [], body, "zh-Hant"
        )
    assert bool(violations) == ("强烈买入" in body)
    assert "市場風險" in full_md and "市场风险" not in full_md
    assert to_traditional(full_md) == full_md
    assert dynamic == to_traditional(dynamic)
    assert to_traditional(_build_footer(_LINKS_PORTFOLIO, "zh")) in full_md
    assert events == ["scan", "scan", "convert", "convert"]
    assert [call.args[1] for call in translate.call_args_list] == ["zh", "zh"]


def test_generate_quiet_zh_hant_stores_traditional(db_session: Session) -> None:
    from app.models.user import User
    from app.services.user_scope import report_language_for
    from app.services.zh_hant import to_traditional

    user = db_session.get(User, _USER)
    assert user is not None
    user.locale = "zh-Hant"
    db_session.flush()
    with (
        patch.object(rg, "compute_portfolio", return_value=_portfolio_snap()),
        patch.object(rg, "load_news_window", return_value=[]),
        patch.object(rg, "detect_macro_signals", return_value=_quiet_signals()),
        patch.object(rg, "detect_window_anomalies", return_value=([], 0)),
        patch.object(rg, "_translate_md", return_value="市场风险值得关注。"),
        patch.object(rg, "_call_llm", side_effect=AssertionError("quiet path must not call LLM")),
    ):
        report = rg.generate_report(
            db_session,
            user_id=_USER,
            report_date=_TODAY,
            output_lang=report_language_for(db_session, _USER, "en"),
        )
    db_session.refresh(report)
    assert report.status == "skipped"
    assert report.report_md is not None
    assert "市場風險" in report.report_md
    assert to_traditional(report.report_md) == report.report_md


def test_generate_zh_hant_violation_suppresses_email(db_session: Session) -> None:
    with contextlib.ExitStack() as stack:
        for mock_patch in _normal_path_patches():
            stack.enter_context(cast(contextlib.AbstractContextManager[object], mock_patch))
        stack.enter_context(patch.object(rg, "_translate_md", return_value="市场风险:强烈买入。"))
        email = stack.enter_context(patch.object(rg, "send_report_email"))
        report = rg.generate_report(
            db_session, user_id=_USER, report_date=_TODAY, output_lang="zh-Hant"
        )
    assert report.status == "needs_review"
    assert report.report_md is not None and "市場風險" in report.report_md
    email.assert_not_called()


@pytest.mark.parametrize("capped", [True, False])
def test_generate_report_window_cap_news_backfill(db_session: Session, capped: bool) -> None:
    from app.core.timezones import ET
    from app.models.news import News
    from app.services.window_data import backfill_news_surfaced_before

    now = datetime(2026, 12, 5, 19, tzinfo=ET) if capped else datetime(2026, 10, 10, 19, tzinfo=ET)
    previous = (
        datetime(2026, 11, 14, 19, 0, 1, tzinfo=ET)
        if capped
        else datetime(2026, 10, 3, 19, 0, 1, tzinfo=ET)
    )
    expected = datetime(2026, 11, 28, tzinfo=ET) if capped else previous
    old = datetime(2026, 11, 20, tzinfo=ET) if capped else datetime(2026, 10, 2, tzinfo=ET)
    recent = datetime(2026, 12, 1, tzinfo=ET) if capped else datetime(2026, 10, 8, tzinfo=ET)
    db_session.add(
        Report(
            user_id=_USER,
            report_date=previous.date(),
            report_type="incremental",
            session_node="after_close",
            status="success",
            period_end=previous,
        )
    )
    for title, published in [("old", old), ("recent", recent), ("boundary", expected)]:
        db_session.add(
            News(
                url_hash=title,
                published_at=published,
                record=build_headline_record(
                    NewsItem(title, title, "", "", published, title), "article", None
                ),
            )
        )
    db_session.flush()
    with (
        patch.object(rg, "datetime", wraps=datetime) as clock,
        patch.object(
            rg, "backfill_news_surfaced_before", wraps=backfill_news_surfaced_before
        ) as backfill,
        patch.object(rg, "compute_portfolio", return_value=_portfolio_snap()),
        patch.object(rg, "detect_macro_signals", return_value=_quiet_signals()),
        patch.object(rg, "detect_window_anomalies", return_value=([], 0)),
        patch.object(rg, "_openrouter_client", return_value=MagicMock()),
        patch.object(rg, "_call_llm", side_effect=_mock_llm),
    ):
        clock.now.return_value = now
        report = rg.generate_report(
            db_session, user_id=_USER, report_date=now.date(), output_lang="en"
        )
    db_session.refresh(report)
    assert report.period_start == expected
    assert report.period_end == now
    assert report.report_inputs is not None
    assert {item["title"] for item in report.report_inputs["news_items"]} == (
        {"recent", "boundary"} if capped else {"old", "recent", "boundary"}
    )
    if capped:
        backfill.assert_called_once_with(db_session, _USER, expected)
    else:
        backfill.assert_not_called()


def test_failed_retry_keeps_window_older_than_floor(db_session: Session) -> None:
    from app.core.timezones import ET

    now = datetime(2026, 12, 5, 19, tzinfo=ET)
    start = datetime(2026, 11, 14, 19, 0, 1, tzinfo=ET)
    end = datetime(2026, 11, 21, 19, tzinfo=ET)
    failed = Report(
        user_id=_USER,
        report_date=now.date(),
        report_type="incremental",
        session_node="manual",
        status="failed",
        period_start=start,
        period_end=end,
    )
    db_session.add(failed)
    db_session.flush()
    with (
        patch.object(rg, "datetime", wraps=datetime) as clock,
        patch.object(rg, "backfill_news_surfaced_before") as backfill,
        patch.object(rg, "compute_portfolio", return_value=_portfolio_snap()),
        patch.object(rg, "detect_macro_signals", return_value=_quiet_signals()),
        patch.object(rg, "detect_window_anomalies", return_value=([], 0)),
    ):
        clock.now.return_value = now
        report = rg.generate_report(
            db_session, user_id=_USER, report_date=now.date(), output_lang="en"
        )
    db_session.refresh(report)
    assert report.id == failed.id
    assert report.period_start == start
    assert report.period_end == end
    backfill.assert_not_called()


def test_large_weight_holding_window_price_reaches_pass2_prompt(db_session: Session) -> None:
    db_session.add_all(
        [
            Holding(
                user_id=_USER,
                name="UK AAPL",
                ticker="AAPL",
                market="UK",
                position=0,
                pricing_mode="auto",
                currency="USD",
            ),
            Holding(
                user_id=_USER,
                name="US AAPL",
                ticker="AAPL",
                market="US",
                position=1,
                pricing_mode="auto",
                currency="USD",
            ),
        ]
    )
    db_session.flush()
    aapl_move = HoldingMove(
        identifier="AAPL",
        market="UK",
        current_price=Decimal("101.22"),
        prev_price=Decimal("100.0"),
        net_pct=Decimal("0.0011"),
        max_day_pct=Decimal("0.0122"),
        max_day_date=_TODAY,
        baseline_date=_TODAY,
        latest_date=_TODAY,
        prev_close=None,
        day_open=None,
        day_high=None,
        day_low=None,
        day_close=None,
        after_hours=None,
    )
    captured: dict[str, str] = {}

    def _capture_pass2(*args: object, **kwargs: object) -> str:
        if kwargs.get("with_holdings"):
            captured["prompt"] = str(args[3])
        return _FAKE_LLM_PASS2

    with (
        patch("app.services.report_generator.compute_portfolio", return_value=_portfolio_snap()),
        patch("app.services.report_generator.load_news_window", return_value=[]),
        patch("app.services.report_generator.detect_macro_signals", return_value=_macro_hit()),
        patch("app.services.report_generator.detect_window_anomalies", return_value=([], 2)),
        patch(
            "app.services.report_generator.resolve_global_moves",
            return_value=({("AAPL", "UK"): aapl_move}, 2),
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_capture_pass2),
    ):
        report = rg.generate_report(db_session, user_id=_USER, report_date=_TODAY)

    assert report.status == "success"
    assert report.report_inputs is not None
    stored = report.report_inputs["large_holding_moves"]
    assert stored["AAPL"]["net_pct"] == pytest.approx(0.0011)
    assert stored["AAPL"]["max_day_pct"] == pytest.approx(0.0122)
    assert stored["AAPL"]["max_day_date"] == _TODAY.isoformat()
    assert "LARGE HOLDINGS WINDOW PRICE" in captured["prompt"]
    assert "AAPL: +0.11% net this report period" in captured["prompt"]
    assert f"largest single day +1.22% on {_TODAY.isoformat()}" in captured["prompt"]


def test_render_full_md_holdings_briefing_header_uses_period_end() -> None:
    from app.core.timezones import ET

    full_md, violations, _translated = rg._render_full_md(
        "2026-10-05",
        _LINKS_PORTFOLIO,
        [],
        "## §2 Macro Events\n\nRates were steady.\n",
        "en",
        period_end=datetime(2026, 10, 5, 16, 0, tzinfo=ET).isoformat(),
    )
    assert full_md.splitlines()[0] == "# Portfonia Holdings Briefing — 2026-10-05 16:00 ET"
    assert not violations


# ---------------------------------------------------------------------------
# Issue #640: removing L1/L2/L3 and assembly must not change Pass 2 prompts
# ---------------------------------------------------------------------------

_PASS2_SNAPSHOT = Path(__file__).parent / "fixtures" / "issue_640_pass2_prompt.json"


def _capture_pass2_prompt(session: Session) -> dict[str, str]:
    captured: dict[str, str] = {}

    def _capture(
        client: object,
        model: str,
        system: str,
        user: str,
        *,
        with_holdings: bool = False,
        **kw: object,
    ) -> str:
        if with_holdings:
            captured["system"] = system
            captured["user"] = user
            return _FAKE_LLM_PASS2
        return _FAKE_LLM_PASS1

    _seed_investment_context(session, _USER, locale="en", intel_focus="GEOPOLITICS")
    with contextlib.ExitStack() as stack:
        for p in _normal_path_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(patch("app.services.report_generator._call_llm", side_effect=_capture))
        stack.enter_context(
            patch("app.services.report_generator.intel_trade_date", return_value=_TODAY)
        )
        report = rg.generate_report(
            session,
            user_id=_USER,
            report_date=_TODAY,
            now=datetime(2026, 6, 4, 21, 0, tzinfo=UTC),
        )
    assert report.status == "success"
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "pass2"
    return captured


def test_issue_640_pass2_prompt_matches_pre_removal_snapshot(db_session: Session) -> None:
    captured = _capture_pass2_prompt(db_session)
    expected = json.loads(_PASS2_SNAPSHOT.read_text(encoding="utf-8"))
    assert captured == expected
