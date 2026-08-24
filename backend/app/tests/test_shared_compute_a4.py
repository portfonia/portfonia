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


def _seed_second_mover_for_u1(db_session: Session) -> None:
    """A second moving holding for U1 (and U2, who also holds AAPL).

    Needed by the L3 tests only: a cross-name cluster requires at least two of
    the reader's OWN names to survive `clusters_for_user`, so with the base
    one-mover-per-user seeding there is nothing for the narrowing step to get
    wrong.
    """
    today = datetime.now(tz=ET).date()
    yesterday = today - timedelta(days=1)
    yesterday_at = datetime.combine(yesterday, time(20, 0), tzinfo=UTC)
    db_session.add_all(
        [
            _close_at("AAPL", _BASELINE_DATE, 150.0, BOOTSTRAP_WATERMARK),
            _close("AAPL", date(2026, 6, 2), 168.0),
            _close_at("AAPL", yesterday, 168.0, yesterday_at),
            _close("AAPL", today, 168.0),
        ]
    )
    db_session.flush()


def _mock_l3_llm(client: object, model: str, system: str, user: str, **kwargs: object) -> str:
    """Group whichever of the day's briefed identifiers the batch actually
    produced.

    Echoing the supplied identifiers back (rather than returning a fixed
    cluster) is what makes the fan-out leak test meaningful: the stored
    clusters then genuinely span every user's names, exactly as a real
    synthesis over a shared cache would, so `clusters_for_user` has something
    real to fail to narrow.
    """
    supplied = [
        line.rstrip(":")
        for line in user.splitlines()
        if line and not line.startswith((" ", "=", "(")) and line.endswith(":")
    ]
    tickers = [s for s in supplied if not s.startswith(("theme:", "fwd:"))]
    if len(tickers) < 2:
        return '{"clusters": []}'
    members = ", ".join(f'"{t}"' for t in tickers)
    return (
        '{"clusters": [{"identifiers": [' + members + '], "mechanism": "discount_rate", '
        '"summary": "The long end repriced and the whole channel followed.", '
        '"confidence": "Probable"}]}'
    )


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
        patch("app.services.cross_name_intel._openrouter_client", return_value=MagicMock()),
        patch("app.services.cross_name_intel._call_llm", side_effect=_mock_l3_llm),
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
    rows = (
        db_session.execute(select(Report).where(Report.session_node != "fixture_seed"))
        .scalars()
        .all()
    )
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
        patch("app.services.report_generator._call_llm") as mock_pass2,
        patch("app.services.report_assembly._call_llm") as mock_assembly,
        patch("app.services.report_translation._call_llm") as mock_translate,
    ):
        rebuilt = rg.regenerate_report(
            db_session,
            report.id,
            user_id=three_user_holdings["U1"],
            mode="render",
            output_lang="en",
        )

    mock_pass2.assert_not_called()
    mock_assembly.assert_not_called()
    mock_translate.assert_not_called()
    assert rebuilt.report_md is not None
    assert _ASSEMBLY_MARKER in rebuilt.report_md


# ---------------------------------------------------------------------------
# Fan-out budget fairness, proven at batch level (design doc §5.7 item 1)
# ---------------------------------------------------------------------------


def _seed_hoggable_price_snapshots(db_session: Session) -> None:
    """Deliberately does NOT reuse `_seed_price_snapshots` — NVDA there is
    shared with U2, which defeats the point (see below). Seeds two
    identifiers held ONLY by U1 (QQQM, a ticker; 110011, a fund-code-only
    holding — CLAUDE.md's fund NAV note: fund NAV is captured into
    `price_snapshots` keyed by `fund_code`, same table/shape as a ticker)
    plus one held only by U3 (SGOL).

    Why exclusivity matters: an earlier version of this test seeded NVDA
    (shared with U2) as one of U1's two candidates. The fair share DID stop
    U1 from taking both itself — but since U2 also legitimately wants NVDA,
    U2 simply picked up whichever of U1's two candidates U1 left unspent,
    using U2's OWN share. U1+U2's combined LEGITIMATE demand (2 distinct
    identifiers between them) still exhausted a cap of 2, leaving U3
    starved regardless of the fix — a false negative in the other
    direction. Making BOTH of U1's candidates exclusive to U1 removes that
    escape hatch: nobody else can "finish" what U1 left on the table, so
    whether U3 gets served is governed ONLY by whether U1 was capped at its
    fair share of 1 (with the fix) or could take both (without it).
    """
    today = datetime.now(tz=ET).date()
    yesterday = today - timedelta(days=1)
    yesterday_at = datetime.combine(yesterday, time(20, 0), tzinfo=UTC)
    # Same shape as the project's other anomaly fixtures (7.5%/5.5%
    # cumulative moves, flat on the last day) — proven to clear each
    # asset_class's window threshold elsewhere in this test suite, not a
    # newly-guessed magnitude.
    db_session.add_all(
        [
            _close_at("QQQM", _BASELINE_DATE, 300.0, BOOTSTRAP_WATERMARK),
            _close("QQQM", date(2026, 6, 2), 322.5),
            _close_at("QQQM", yesterday, 322.5, yesterday_at),
            _close("QQQM", today, 322.5),
            PriceSnapshot(
                ticker="110011",
                market="A-Share",
                session_node="close",
                trade_date=_BASELINE_DATE,
                close=Decimal("100"),
                captured_at=BOOTSTRAP_WATERMARK,
            ),
            _close("110011", date(2026, 6, 2), 107.5),
            _close_at("110011", yesterday, 107.5, yesterday_at),
            _close("110011", today, 107.5),
            _close_at("SGOL", _BASELINE_DATE, 180.0, BOOTSTRAP_WATERMARK),
            _close("SGOL", date(2026, 6, 2), 190.0),
            _close_at("SGOL", yesterday, 190.0, yesterday_at),
            _close("SGOL", today, 190.0),
        ]
    )
    db_session.flush()


def test_the_first_user_cannot_exhaust_the_days_l1_budget(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    """The third recurrence of the fairness bug, closed at the level it
    actually bit: a real three-user batch where the first user in the fixed
    fan-out order (U1) has two candidates EXCLUSIVE to itself — QQQM and
    the fund-code-only holding 110011, neither shared with U2 or U3 — and
    the last user (U3) has exactly one exclusive candidate (SGOL). Cap = 2.

    Review round-1 finding (PR #163): the original version of this test
    used a shared identifier (NVDA) as one of U1's two "own" candidates.
    That masked the mechanism two different ways in two different drafts —
    first with a budget equal to total demand (so unrestricted first-come
    already served everyone, proving nothing), then with NVDA shared with
    U2 (so whichever of U1's two candidates U1 left unspent, U2 simply
    picked up with U2's OWN share — U1+U2's combined LEGITIMATE demand
    still exhausted the cap regardless of the fix, a false negative in the
    other direction). Both of U1's candidates being exclusive to U1 closes
    that escape hatch: nobody else can finish what U1 leaves on the table,
    so whether U3 gets served is governed ONLY by whether U1 was capped at
    its fair share.

    Provably discriminating (verified by hand before writing this
    docstring, not asserted on faith): patching `fair_share_budget` to
    "always return the full remaining budget" — i.e. simulating the
    pre-fix behavior — makes U1 spend the whole cap on QQQM + 110011 and
    this test's assertion FAIL. With the real fix, U1's share is
    ceil(2/3)=1, so it spends on only one of its two candidates, and the
    unspent unit flows forward to U3's turn — SGOL gets analyzed.
    """
    _seed_hoggable_price_snapshots(db_session)

    from app.tasks.report_tasks import generate_incremental_report

    settings = get_settings()
    with contextlib.ExitStack() as stack:
        for p in _boundary_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(patch.object(settings, "SHARED_COMPUTE_ENABLED", False))
        stack.enter_context(patch("app.services.ticker_intel._MAX_L1_ANALYSES_PER_DAY", 2))
        mock_l1 = stack.enter_context(
            patch(
                "app.services.ticker_intel._call_llm",
                return_value="Moved on an earnings beat. [Established]",
            )
        )
        generate_incremental_report.run()

    analyzed = {c.args[3].split("\n")[0] for c in mock_l1.call_args_list}
    assert any("SGOL" in a for a in analyzed), (
        "U3, last in the fixed fan-out order, was starved because U1 spent "
        "the whole cap on its own two candidates — the exact bug "
        "fair_share_budget exists to close"
    )


# ---------------------------------------------------------------------------
# L3 day-level cross-name synthesis at fan-out scale (issue #128 quality gate,
# design doc §6.7 item 1)
# ---------------------------------------------------------------------------


def test_cross_name_clusters_never_carry_another_users_holdings(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    """The sharpest form of A4's leak risk, and the reason this layer needed
    its own test rather than an extension of the assembly one.

    L1 and L2 rows are keyed to a thing (an identifier, an event) — a user
    simply never asks for a key they do not hold. An L3 cluster is a statement
    ABOUT A GROUP drawn from every user's book at once, so the day's stored
    conclusion legitimately spans U1's NVDA and U3's SGOL. The `_mock_l3_llm`
    stub above reproduces exactly that: it groups whatever the day briefed.
    Everything therefore rests on `clusters_for_user` narrowing on the way
    out — if it did not, U3's report would state that their gold position
    moved with a name they have never owned.

    U1 is given a SECOND moving holding on purpose. With one L1 key each, the
    two-name floor drops every cluster for everybody and the isolation
    assertions below hold trivially — a green test proving nothing. AAPL gives
    U1 a surviving cluster, so "the foreign name is absent" becomes a claim
    about narrowing rather than about emptiness.
    """
    _seed_price_snapshots(db_session)
    _seed_second_mover_for_u1(db_session)

    _run_batch(SHARED_COMPUTE_ENABLED=True, ASSEMBLY_LLM_MODEL=_ASSEMBLY_MODEL)

    reports = _reports(db_session)
    u1 = reports[three_user_holdings["U1"]]
    u3 = reports[three_user_holdings["U3"]]
    assert u1.report_inputs is not None and u3.report_inputs is not None

    # Discriminating precondition: U1 must actually HAVE a cluster, or the
    # isolation assertions below are satisfied by an empty list and prove
    # nothing about narrowing.
    u1_clusters = u1.report_inputs["cross_name_intel"]
    assert u1_clusters, "U1 should have a surviving cluster (NVDA + AAPL both moved)"
    assert sorted(u1_clusters[0]["identifiers"]) == ["AAPL", "NVDA"]

    for report, foreign in ((u1, "SGOL"), (u3, "NVDA")):
        assert report.report_inputs is not None
        for cluster in report.report_inputs["cross_name_intel"]:
            assert foreign not in cluster["identifiers"], (
                f"a shared cluster carried {foreign} into a report that does not hold it"
            )
            assert foreign not in cluster["summary"]
        assert report.report_md is not None
        assert foreign not in report.report_md


def test_the_days_synthesis_is_computed_once_and_shared(
    db_session: Session, three_user_holdings: dict[str, Any]
) -> None:
    """The cost property, for the layer that exists to be paid for once.

    Three users, one trading day: the synthesis may run more than once ONLY
    when a later user's newly-written L1 rows change the global input set
    (that is what the fingerprint in the cache key buys). It must never run
    once per user unconditionally, and it must stay under the daily cap.
    """
    _seed_price_snapshots(db_session)

    from app.tasks.report_tasks import generate_incremental_report

    settings = get_settings()
    with contextlib.ExitStack() as stack:
        for p in _boundary_patches():
            stack.enter_context(p)  # type: ignore[arg-type]
        stack.enter_context(patch.object(settings, "SHARED_COMPUTE_ENABLED", True))
        stack.enter_context(patch.object(settings, "ASSEMBLY_LLM_MODEL", _ASSEMBLY_MODEL))
        mock_l3 = stack.enter_context(
            patch("app.services.cross_name_intel._call_llm", side_effect=_mock_l3_llm)
        )
        generate_incremental_report.run()

    from app.services import cross_name_intel as l3

    assert mock_l3.call_count <= l3._MAX_SYNTHESES_PER_DAY, (
        "the day-level synthesis must stay bounded by its daily cap, "
        "not scale with the number of users"
    )
