"""End-to-end UAT for Ring 1 stage A4 (issue #128), design doc §7.2 UAT-8/9:
Hermes/Portfonia/Docs/Ring 1-A design.md.

Same style as test_shared_compute_a1/_a2/_a3.py: drives the real
`generate_incremental_report.run()` fan-out over the real three-user fixture
against a real `db_session`, mocking only the LLM/HTTP boundary.

A4's own risk is the one the earlier checkpoints did not have. A2/A3 had to
keep per-user values OUT of a shared cache; A4 reads two shared caches and
mixes in per-user holdings, so its failure mode is the mirror image —
another user's shared rows reaching this user's report. That is design doc
§1.3's cross-user leak arriving at the last checkpoint, and it is what
`test_assembled_reports_never_carry_another_users_holdings` exists to catch
at the fan-out level rather than in a unit test's mocked context.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import ET
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.services import report_generator as rg
from app.services.window_data import BOOTSTRAP_WATERMARK

_BASELINE_DATE = date(2026, 6, 1)
_ASSEMBLY_MODEL = "shadow/cheap"

_PASS2_FILLER = "Filler context. " * 130
_PASS2_MARKER = "ZZZ_PASS2_BODY_ZZZ"
_ASSEMBLY_MARKER = "ZZZ_ASSEMBLED_BODY_ZZZ"

_FAKE_PASS2_BODY = (
    f"## §2 Macro Signals\n\n{_PASS2_MARKER} nothing macro.\n\n"
    "## §3 Holdings Intelligence\n\nSee anomalies.\n\n"
    "## §4 Risk Radar\n\nSee anomalies.\n\n" + _PASS2_FILLER
)
_FAKE_ASSEMBLED_BODY = (
    f"## §2 Macro Signals\n\n{_ASSEMBLY_MARKER} rates repriced.\n\n"
    "## §3 Holdings Analysis\n\nThe heaviest position moved. [Established]\n\n"
    "## §4 Risk Radar\n\nSee anomalies. [Established]\n\n" + _PASS2_FILLER
)


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
    """Give every user at least one moving holding, so nobody short-circuits
    on the quiet-day skip (which returns before the body pass runs at all).

    NVDA belongs to U1+U2, SGOL only to U3 — the overlap/disjoint split the
    isolation assertions below rely on.
    """
    today = datetime.now(tz=ET).date()
    yesterday = today - timedelta(days=1)
    yesterday_at = datetime.combine(yesterday, time(20, 0), tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("NVDA", _BASELINE_DATE, 200.0, BOOTSTRAP_WATERMARK),
            _close("NVDA", date(2026, 6, 2), 215.0),
            _close_at("NVDA", yesterday, 215.0, yesterday_at),
            _close("NVDA", today, 215.0),
            _close_at("SGOL", _BASELINE_DATE, 180.0, BOOTSTRAP_WATERMARK),
            _close("SGOL", date(2026, 6, 2), 190.0),
            _close_at("SGOL", yesterday, 190.0, yesterday_at),
            _close("SGOL", today, 190.0),
        ]
    )
    db_session.flush()


def _mock_report_llm(client: object, model: str, system: str, user: str, **kwargs: object) -> str:
    if kwargs.get("with_holdings"):
        return _FAKE_PASS2_BODY
    return '{"queries": []}'


def _boundary_patches() -> list[object]:
    """Every LLM/HTTP boundary the fan-out can reach. Each module resolves its
    own `_call_llm`, so they are patched independently (same reason
    test_report_generator.py has separate L1/L2 fixtures). Individual tests
    re-patch a specific one on top of these to assert on its calls."""
    return [
        patch("app.services.report_generator._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_generator._call_llm", side_effect=_mock_report_llm),
        patch("app.services.report_generator._run_tavily_search", return_value=[]),
        patch("app.services.report_translation._openrouter_client", return_value=MagicMock()),
        patch("app.services.report_translation._call_llm", side_effect=_mock_report_llm),
        patch("app.services.report_translation.time.sleep"),
        patch("app.services.ticker_intel._openrouter_client", return_value=MagicMock()),
        patch(
            "app.services.ticker_intel._call_llm",
            return_value="Moved on an earnings beat. [Established]",
        ),
        patch("app.services.macro_event_intel._openrouter_client", return_value=MagicMock()),
        patch(
            "app.services.macro_event_intel._call_llm",
            return_value='{"analysis": "Policy steady. [Established]", '
            '"affected_asset_classes": ["EQUITY_US_TECH"], "affected_sectors": []}',
        ),
        patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY),
    ]


def _run_batch(**setting_overrides: object) -> dict[str, Any]:
    from app.tasks.report_tasks import generate_incremental_report

    settings = get_settings()
    with contextlib.ExitStack() as stack:
        for p in _boundary_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        for name, value in setting_overrides.items():
            stack.enter_context(patch.object(settings, name, value))
        result: dict[str, Any] = generate_incremental_report.run()
    return result


def _reports(db_session: Session) -> dict[Any, Report]:
    rows = db_session.execute(select(Report)).scalars().all()
    return {r.user_id: r for r in rows}


# ---------------------------------------------------------------------------
# The A4-specific leak risk: reading two shared caches per user
# ---------------------------------------------------------------------------


def test_assembled_reports_never_carry_another_users_holdings(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    """Design doc §8.2 hard gate, at the checkpoint where it is easiest to
    break: the shared caches hold the whole day's identifiers for everybody,
    so an assembly step that read them by date instead of through this user's
    own scoped `ctx` would hand U3 a briefing about U1's NVDA.

    U1/U2 hold NVDA; only U3 holds SGOL. Neither may appear in the other's
    assembled prompt or rendered body.
    """
    _seed_price_snapshots(db_session)

    _run_batch(SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL=_ASSEMBLY_MODEL)

    reports = _reports(db_session)
    assert len(reports) == 3

    u1 = reports[three_user_holdings["U1"]]
    u3 = reports[three_user_holdings["U3"]]
    assert u1.report_inputs is not None and u3.report_inputs is not None

    u1_prompt = u1.report_inputs.get("assembly_prompt", "")
    u3_prompt = u3.report_inputs.get("assembly_prompt", "")
    assert u1_prompt and u3_prompt, "both users should have taken the assembly path"

    assert "NVDA" in u1_prompt
    assert "SGOL" not in u1_prompt, "U1's assembly saw a holding only U3 owns"
    assert "SGOL" in u3_prompt
    assert "NVDA" not in u3_prompt, "U3's assembly saw a holding only U1/U2 own"

    assert u1.report_md is not None and u3.report_md is not None
    assert "SGOL" not in u1.report_md
    assert "NVDA" not in u3.report_md


def test_shared_ticker_intel_is_analyzed_once_but_assembled_per_user(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    """The whole point of the two-layer split, visible in one run: NVDA is
    analyzed ONCE for the system (U1 and U2 share the L1 row), while the
    assembly pass runs once PER user — `O(|identifiers|) + O(N)`, which is
    design doc §8.2's structural cost property."""
    _seed_price_snapshots(db_session)

    from app.tasks.report_tasks import generate_incremental_report

    settings = get_settings()
    with contextlib.ExitStack() as stack:
        for p in _boundary_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(patch.object(settings, "SHARED_COMPUTE_ENABLED", True))
        stack.enter_context(patch.object(settings, "ASSEMBLY_LLM_MODEL", _ASSEMBLY_MODEL))
        mock_l1 = stack.enter_context(
            patch(
                "app.services.ticker_intel._call_llm",
                return_value="Moved on an earnings beat. [Established]",
            )
        )
        mock_assembly = stack.enter_context(
            patch("app.services.report_assembly._call_llm", return_value=_FAKE_ASSEMBLED_BODY)
        )
        generate_incremental_report.run()

    nvda_calls = [c for c in mock_l1.call_args_list if "NVDA" in c.args[3]]
    assert len(nvda_calls) == 1, "NVDA must be analyzed once for the whole system"
    assert mock_assembly.call_count == 3, "assembly is per-user by design"


# ---------------------------------------------------------------------------
# UAT-8: degradation (design doc §7.2)
# ---------------------------------------------------------------------------


def test_uat8_cold_caches_fall_back_to_pass2_for_every_user(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    """UAT-8: with both shared caches empty the batch degrades to the pre-A4
    body pass — every user still gets a report, and it is the Pass 2 one."""
    _seed_price_snapshots(db_session)

    from app.tasks.report_tasks import generate_incremental_report

    settings = get_settings()
    with contextlib.ExitStack() as stack:
        for p in _boundary_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(patch.object(settings, "SHARED_COMPUTE_ENABLED", True))
        stack.enter_context(patch.object(settings, "ASSEMBLY_LLM_MODEL", _ASSEMBLY_MODEL))
        stack.enter_context(
            patch("app.services.report_generator.get_l1_intel_batch", return_value={})
        )
        stack.enter_context(
            patch("app.services.report_generator.get_l2_intel_batch", return_value={})
        )
        mock_assembly = stack.enter_context(patch("app.services.report_assembly._call_llm"))
        result = generate_incremental_report.run()

    assert result["status"] == "completed"
    mock_assembly.assert_not_called()
    for report in _reports(db_session).values():
        assert report.report_inputs is not None
        assert report.report_inputs["body_source"] == "pass2"
        assert report.report_md is not None
        assert _PASS2_MARKER in report.report_md


def test_uat8_disabled_switch_reproduces_the_pre_a4_batch(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    """Design doc §6.5: `SHARED_COMPUTE_ENABLED=false` must be
    indistinguishable from the pre-A4 pipeline, so the switch is a real
    rollback and not just a different code path with similar output."""
    _seed_price_snapshots(db_session)

    result = _run_batch(SHARED_COMPUTE_ENABLED=False, ASSEMBLY_LLM_MODEL=_ASSEMBLY_MODEL)

    assert result["status"] == "completed"
    for report in _reports(db_session).values():
        assert report.report_inputs is not None
        assert report.report_inputs["body_source"] == "pass2"
        assert report.report_inputs["assembly_raw"] == ""
        assert report.report_md is not None
        assert _ASSEMBLY_MARKER not in report.report_md


# ---------------------------------------------------------------------------
# UAT-9: re-render contract (design doc §7.2, issue #6)
# ---------------------------------------------------------------------------


def test_uat9_assembled_report_rerenders_with_zero_llm_calls(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    """UAT-9: an assembly-sourced report must still rebuild from stored
    inputs with no new LLM call on any pass — the #6 contract A4 is not
    allowed to break."""
    _seed_price_snapshots(db_session)
    _run_batch(SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL=_ASSEMBLY_MODEL)

    report = _reports(db_session)[three_user_holdings["U1"]]
    assert report.report_inputs is not None
    assert report.report_inputs["body_source"] == "assembly"

    with (
        patch(
            "app.services.report_generator.get_current_user_id",
            return_value=three_user_holdings["U1"],
        ),
        patch("app.services.report_generator._call_llm") as mock_pass2,
        patch("app.services.report_assembly._call_llm") as mock_assembly,
        patch("app.services.report_translation._call_llm") as mock_translate,
    ):
        rebuilt = rg.regenerate_report(db_session, report.id, mode="render", output_lang="en")

    mock_pass2.assert_not_called()
    mock_assembly.assert_not_called()
    mock_translate.assert_not_called()
    assert rebuilt.report_md is not None
    assert _ASSEMBLY_MARKER in rebuilt.report_md


# ---------------------------------------------------------------------------
# Fan-out budget fairness, proven at batch level (design doc §5.7 item 1)
# ---------------------------------------------------------------------------


def test_the_first_user_cannot_exhaust_the_days_l1_budget(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    """The third recurrence of the fairness bug, closed at the level it
    actually bit: a real three-user batch with a cap of 1. Pre-fix, the first
    user took the single slot and the same later users starved every day.
    With the share, a cap of 1 across 3 users gives each user one slot of its
    own — so no user is systematically last."""
    _seed_price_snapshots(db_session)

    from app.tasks.report_tasks import generate_incremental_report

    settings = get_settings()
    with contextlib.ExitStack() as stack:
        for p in _boundary_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(patch.object(settings, "SHARED_COMPUTE_ENABLED", False))
        stack.enter_context(patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 3))
        mock_l1 = stack.enter_context(
            patch(
                "app.services.ticker_intel._call_llm",
                return_value="Moved on an earnings beat. [Established]",
            )
        )
        generate_incremental_report.run()

    # U1/U2 share NVDA (one analysis, cached for the second), U3's SGOL is
    # its own — the day's 3 slots were not consumed by whoever ran first.
    analyzed = {c.args[3].split("\n")[0] for c in mock_l1.call_args_list}
    assert any("NVDA" in a for a in analyzed)
    assert any("SGOL" in a for a in analyzed), (
        "U3, last in the fixed fan-out order, still got a fresh analysis"
    )
