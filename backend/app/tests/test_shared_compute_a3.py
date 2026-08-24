"""End-to-end UAT for Ring 1 stage A3 (issue #128), design doc §7.2 UAT-7:
Hermes/Portfonia/Docs/Ring 1-A design.md.

Same style as test_shared_compute_a1.py/_a2.py: runs
`generate_incremental_report.run()` against the REAL three-user fixture
through a REAL `db_session`, mocking only the LLM/HTTP boundary — so the
cross-user sharing property is proven at the fan-out level, not just in
macro_event_intel.py's unit tests.

Unlike _a2.py, `detect_macro_signals` is NOT mocked away: a real news row is
seeded so the real keyword detector fires the same theme for all three
users, which is precisely the condition L2 has to collapse into one
inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.macro_event_intel import MacroEventIntel
from app.models.news import News
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.services.portfolio_calculator import Concentration, PortfolioSnapshot
from app.services.window_data import BOOTSTRAP_WATERMARK

_BASELINE_DATE = date(2026, 6, 1)
_BASELINE_AT = BOOTSTRAP_WATERMARK

_L2_MARKER = "ZZZ_L2_SHARED_EVENT_MARKER_ZZZ"


def _close_at(ticker: str, d: date, close: float, captured_at: datetime) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker,
        market="US",
        session_node="close",
        trade_date=d,
        close=Decimal(str(close)),
        captured_at=captured_at,
    )


def _close(ticker: str, d: date, close: float) -> PriceSnapshot:
    return PriceSnapshot(
        ticker=ticker, market="US", session_node="close", trade_date=d, close=Decimal(str(close))
    )


def _seed_price_snapshots(db_session: Session) -> None:
    """Same shape as _a2.py's fixture: every user gets at least one anomaly so
    nobody hits the quiet-day skip (which returns before Pass 1 and L1/L2 run
    at all)."""
    today = datetime.now(tz=ET).date()
    yesterday = today - timedelta(days=1)
    yesterday_close_at = datetime.combine(yesterday, time(20, 0), tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("NVDA", _BASELINE_DATE, 200.0, _BASELINE_AT),
            _close("NVDA", date(2026, 6, 2), 215.0),
            _close_at("NVDA", yesterday, 215.0, yesterday_close_at),
            _close("NVDA", today, 215.0),
            _close_at("SGOL", _BASELINE_DATE, 180.0, _BASELINE_AT),
            _close("SGOL", date(2026, 6, 2), 190.0),
            _close_at("SGOL", yesterday, 190.0, yesterday_close_at),
            _close("SGOL", today, 190.0),
        ]
    )
    db_session.flush()


def _seed_day_news(db_session: Session) -> None:
    """One story published today that the REAL keyword table matches to the
    货币政策 theme ("Fed" is a word-boundary keyword). Every user's own
    `load_news_window` sees it too (none of them has surfaced it yet), so all
    three select the same theme — the exact overlap L2 must collapse."""
    now = datetime.now(tz=ET)
    # Must satisfy BOTH selectors at once, and `_run_batch` uses the real
    # clock: `load_news_window` takes `published_at <= period_end` (= now, so
    # a fixed noon stamp is in the FUTURE for any run before noon ET and the
    # theme never hits), while `load_day_news` takes today's ET calendar day
    # (so subtracting an hour must not fall off the back of midnight).
    published = max(now - timedelta(hours=1), datetime.combine(now.date(), time.min, tzinfo=ET))
    db_session.add(
        News(
            url_hash="https://x.test/fed",
            title="Fed holds rates steady at its latest meeting",
            source="S",
            url="https://x.test/fed",
            summary="",
            published_at=published,
        )
    )
    db_session.flush()


def _empty_portfolio_snap() -> PortfolioSnapshot:
    """Zero holdings but a non-empty by_asset_class: `user_event_exposure`
    intersects against these keys, and an empty map would make the per-user
    half trivially empty for every user regardless of the cached classes."""
    return PortfolioSnapshot(
        base_currency="USD",
        fx_date=date(2026, 6, 6),
        holdings=[],
        total_base=Decimal("0"),
        by_currency={},
        by_asset_type={},
        by_market={},
        by_sector={},
        by_asset_class={"EQUITY_US_BROAD": Decimal("100")},
        concentration=Concentration(
            top_holding_name="",
            top_holding_ratio=Decimal("0"),
            top_holding_asset_class="",
            top3_ratio=Decimal("0"),
            top_asset_class_name="",
            top_asset_class_ratio=Decimal("0"),
            single_holding_watch=False,
            single_holding_high=False,
            top3_watch=False,
            asset_class_watch=False,
            asset_class_high=False,
        ),
        stale_tickers=[],
    )


_PASS2_FILLER = "Filler context. " * 130
_FAKE_LLM_PASS2 = (
    "## §2 Macro Signals\n\nNothing macro.\n\n"
    "## §3 Holdings Intelligence\n\nSee anomalies.\n\n"
    "## §4 Risk Radar\n\nSee anomalies.\n\n" + _PASS2_FILLER
)


def _mock_report_llm(client: object, model: str, system: str, user: str, **kwargs: object) -> str:
    if kwargs.get("with_holdings"):
        return _FAKE_LLM_PASS2
    return '{"queries": []}'


def _mock_l2_llm(*args: object, **kwargs: object) -> str:
    import json

    return json.dumps(
        {
            "analysis": f"{_L2_MARKER} the policy meeting left rates unchanged. [Established]",
            "affected_asset_classes": ["EQUITY_US_BROAD", "NOT_A_REAL_CLASS"],
            "affected_sectors": ["Financials"],
        }
    )


@dataclass
class _BatchOutcome:
    result: dict[str, Any]
    mock_l2: MagicMock


def _run_batch(l2_call_side_effect: object = _mock_l2_llm) -> _BatchOutcome:
    from app.tasks.report_tasks import generate_incremental_report

    with (
        patch(
            "app.services.report_generator.compute_portfolio", return_value=_empty_portfolio_snap()
        ),
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_report_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        patch("app.services.report_translation._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_translation._call_llm", side_effect=_mock_report_llm),
        patch("app.services.report_translation.time.sleep"),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch(
            "app.services.ticker_intel._call_llm",
            return_value="Nothing notable. [Speculative]",
        ),
        patch("app.services.macro_event_intel._openrouter_client", return_value=MagicMock()),
        patch(
            "app.services.macro_event_intel._call_llm", side_effect=l2_call_side_effect
        ) as mock_l2,
    ):
        result = generate_incremental_report.run()

    return _BatchOutcome(result=result, mock_l2=mock_l2)


def test_same_macro_event_is_inferred_once_across_the_whole_batch(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """UAT-7, first half (design doc §5.6 hard gate): three users all hit the
    货币政策 theme in the same batch — the L2 inference must fire exactly
    once, and leave exactly one cache row."""
    _seed_price_snapshots(db_session)
    _seed_day_news(db_session)

    outcome = _run_batch()

    assert outcome.result["status"] == "completed"
    assert len(outcome.result["results"]) == 3

    theme_calls = [c for c in outcome.mock_l2.call_args_list if "货币政策" in c.args[3]]
    assert len(theme_calls) == 1

    rows = (
        db_session.execute(
            select(MacroEventIntel).where(MacroEventIntel.event_key == "theme:货币政策")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_every_user_in_the_batch_reads_the_same_shared_analysis(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """The flip side of "inferred once": the two users who did NOT pay for the
    inference must still receive it, byte-identical — otherwise the cache
    saves cost by silently degrading everyone downstream of the first user."""
    _seed_price_snapshots(db_session)
    _seed_day_news(db_session)

    _run_batch()

    reports = (
        db_session.execute(select(Report).where(Report.session_node != "fixture_seed"))
        .scalars()
        .all()
    )
    analyses = {
        r.user_id: (r.report_inputs or {}).get("macro_event_intel", {}).get("theme:货币政策", {})
        for r in reports
    }
    assert len(analyses) == 3
    assert all(_L2_MARKER in a.get("analysis", "") for a in analyses.values())
    assert len({a["analysis"] for a in analyses.values()}) == 1


def test_out_of_taxonomy_class_never_reaches_any_users_exposure(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """UAT-7, second half: the model proposed NOT_A_REAL_CLASS alongside a
    valid class. The invented label must be dropped before storage, so no
    user's exposure mapping can ever see it."""
    _seed_price_snapshots(db_session)
    _seed_day_news(db_session)

    _run_batch()

    row = (
        db_session.execute(
            select(MacroEventIntel).where(MacroEventIntel.event_key == "theme:货币政策")
        )
        .scalars()
        .one()
    )
    assert row.affected_asset_classes == ["EQUITY_US_BROAD"]

    reports = (
        db_session.execute(select(Report).where(Report.session_node != "fixture_seed"))
        .scalars()
        .all()
    )
    for r in reports:
        inputs = r.report_inputs or {}
        assert inputs["macro_event_exposure"] == {"theme:货币政策": ["EQUITY_US_BROAD"]}
        assert "NOT_A_REAL_CLASS" not in str(inputs["macro_event_intel"])


def test_l2_failure_does_not_break_any_users_report(
    db_session: Session, three_user_holdings: dict[str, object]
) -> None:
    """Degrade, don't fail: an event with no usable inference costs that event
    its intel, not the batch."""
    _seed_price_snapshots(db_session)
    _seed_day_news(db_session)

    def _boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("provider down")

    outcome = _run_batch(_boom)

    assert outcome.result["status"] == "completed"
    reports = (
        db_session.execute(select(Report).where(Report.session_node != "fixture_seed"))
        .scalars()
        .all()
    )
    assert len(reports) == 3
    assert all(r.status == "success" for r in reports)
    # One marker row for the whole batch — the failure is not re-attempted
    # once per user.
    assert outcome.mock_l2.call_count == 1
