"""L3 day-level cross-identifier synthesis (issue #128 quality gate — design
doc §6.7 item 1).

The gap this closes: L1 is keyed per identifier, L2 per event. Neither can
express "these identifiers moved together today for one mechanism", which is
the joint inference Pass 2 performs inside its single call and which assembly
is contractually forbidden to invent. This layer performs that inference ONCE
per trading day, globally, and assembly receives only the intersection with
the calling user's own L1 keys.

The two principles A2/A3 established (design doc §4.8) apply with a twist
worth stating, because getting it wrong here is a cross-user leak in the
report BODY rather than in a cache row:

1. Selection may be per-user; values must be global. Here there is no
   selection channel at all — `get_day_synthesis` takes (session, trade_date)
   and derives its inputs from global tables itself, so no per-user value has
   a parameter to arrive through.
   -> test_signature_cannot_accept_per_user_state (structural)
   -> test_inputs_come_from_every_users_l1_rows_not_the_callers
2. Whatever a shared entry describes must be a pure function of the date.
   -> test_synthesis_is_day_scoped_not_window_scoped

And the one that is new to this layer:

3. The output must be DECOMPOSABLE per identifier, or the per-user filter has
   nothing to filter. A single day-level paragraph naming every identifier
   analyzed today cannot be narrowed to one user's book — it would carry
   other users' holdings into this user's report.
   -> test_cluster_summary_naming_an_unheld_identifier_is_dropped_for_that_user
"""

from __future__ import annotations

import inspect
import json
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import openai
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.cross_name_intel import CrossNameIntel
from app.models.macro_event_intel import MacroEventIntel
from app.models.ticker_intel import TickerIntel
from app.services import cross_name_intel as l3
from app.services import macro_event_intel as l2_module
from app.services import ticker_intel as l1_module

_DATE = date(2026, 8, 17)


# ---------------------------------------------------------------------------
# Seeding helpers — every one of these writes a GLOBAL row (no user_id exists
# on any of these tables), which is the property the tests below lean on.
# ---------------------------------------------------------------------------


def _seed_l1(
    session: Session,
    identifier: str,
    analysis: str | None = "It rose on supply-chain news. [Probable]",
    trade_date: date = _DATE,
    prompt_version: str = l1_module._PROMPT_VERSION,
) -> TickerIntel:
    row = TickerIntel(
        identifier=identifier,
        trade_date=trade_date,
        prompt_version=prompt_version,
        model="openai/gpt-5.6-luna",
        analysis=analysis,
        attempt_count=1,
        facts={"day_pct": 0.012},
    )
    session.add(row)
    session.flush()
    return row


def _seed_l2(
    session: Session,
    event_key: str = "theme:monetary_policy",
    analysis: str | None = "Long-end yields rose after the auction. [Established]",
    trade_date: date = _DATE,
    prompt_version: str = l2_module._PROMPT_VERSION,
) -> MacroEventIntel:
    row = MacroEventIntel(
        event_key=event_key,
        trade_date=trade_date,
        prompt_version=prompt_version,
        model="openai/gpt-5.6-luna",
        analysis=analysis,
        attempt_count=1,
        affected_asset_classes=["EQUITY_US_TECH"],
        affected_sectors=[],
        facts={},
    )
    session.add(row)
    session.flush()
    return row


def _response(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = json.dumps(payload)
    resp.choices[0].finish_reason = "stop"
    resp.model = "openai/gpt-5.6-luna"
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=10, total_tokens=20)
    return resp


def _cluster(
    identifiers: list[str],
    mechanism: str = "ai_capex_stack",
    summary: str = "Accelerator demand drove the group while rates cut the other way.",
    confidence: str = "Probable",
) -> dict[str, Any]:
    return {
        "identifiers": identifiers,
        "mechanism": mechanism,
        "summary": summary,
        "confidence": confidence,
    }


def _patched_llm(payload: dict[str, Any]) -> Any:
    """Patch the OpenAI client so `_call_llm` returns `payload` as JSON."""
    client = MagicMock()
    client.chat.completions.create.return_value = _response(payload)
    return patch.object(l3, "_openrouter_client", return_value=client)


# ---------------------------------------------------------------------------
# 1. The type boundary — structural, so a future edit fails at import time
# ---------------------------------------------------------------------------


def test_signature_cannot_accept_per_user_state() -> None:
    """`get_day_synthesis` must have no parameter through which a user_id, a
    watermark, a portfolio or an anomaly list could arrive.

    L1/L2 each needed a narrow per-user selection channel returning
    `list[str]`. This layer needs none: what it analyzes is "every identifier
    the system briefed today", which is already a global fact. So the boundary
    is stronger and can be asserted directly on the signature.
    """
    params = set(inspect.signature(l3.get_day_synthesis).parameters)
    forbidden = {
        "user_id",
        "period_start",
        "period_end",
        "portfolio",
        "portfolio_summary",
        "anomalies",
        "price_anomalies",
        "holdings",
        "ctx",
    }
    assert not (params & forbidden), f"per-user parameter leaked into L3: {params & forbidden}"


def test_module_imports_no_per_user_module() -> None:
    """The shared writer must not reach the per-user assembly/generator layer.

    Same rule `report_assembly` is held to from the other side (it imports no
    ORM model): with no import of a per-user module there is no helper here
    that could re-derive one user's book.

    Checked on IMPORT STATEMENTS, not on the source text, so the module may
    still name these modules in prose when explaining why it does not use
    them — which is exactly what its docstring does.
    """
    import_lines = [
        line for line in inspect.getsource(l3).splitlines() if line.startswith(("import ", "from "))
    ]
    for banned in ("report_assembly", "report_generator", "user_watermark"):
        offenders = [line for line in import_lines if banned in line]
        assert not offenders, f"L3 must not import {banned}: {offenders}"


# ---------------------------------------------------------------------------
# 2. Inputs are global and day-scoped
# ---------------------------------------------------------------------------


def test_inputs_come_from_every_users_l1_rows_not_the_callers(db_session: Session) -> None:
    """Two users' identifiers were briefed today; the synthesis prompt sees
    BOTH, regardless of who triggered it.

    This is the property that makes one call per day serve everybody. If the
    prompt were built from the calling user's `ctx.ticker_intel`, the first
    user to run would cache a synthesis covering only their own book and every
    later user would read a conclusion that cannot mention any of their names.
    """
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")
    _seed_l1(db_session, "SGOL")  # only the OTHER user holds this one

    captured: dict[str, str] = {}
    client = MagicMock()
    client.chat.completions.create.return_value = _response(
        {"clusters": [_cluster(["TSM", "ASML"])]}
    )

    def _capture(*args: Any, **kwargs: Any) -> Any:
        captured["prompt"] = args[3] if len(args) > 3 else kwargs["user_prompt"]
        return client.chat.completions.create.return_value.choices[0].message.content

    with (
        patch.object(l3, "_openrouter_client", return_value=client),
        patch.object(l3, "_call_llm", side_effect=_capture),
    ):
        l3.get_day_synthesis(db_session, _DATE)

    assert "TSM" in captured["prompt"]
    assert "ASML" in captured["prompt"]
    assert "SGOL" in captured["prompt"]


def test_synthesis_is_day_scoped_not_window_scoped(db_session: Session) -> None:
    """Yesterday's L1 rows must not bleed into today's synthesis.

    Design doc §4.8's round-5 lesson, restated: the only date this layer knows
    is `trade_date`. There is no window to be per-user about.
    """
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")
    _seed_l1(db_session, "OLDNAME", trade_date=_DATE - timedelta(days=1))

    captured: dict[str, str] = {}

    def _capture(*args: Any, **kwargs: Any) -> Any:
        captured["prompt"] = args[3] if len(args) > 3 else kwargs["user_prompt"]
        return json.dumps({"clusters": []})

    with (
        patch.object(l3, "_openrouter_client", return_value=MagicMock()),
        patch.object(l3, "_call_llm", side_effect=_capture),
    ):
        l3.get_day_synthesis(db_session, _DATE)

    assert "TSM" in captured["prompt"]
    assert "OLDNAME" not in captured["prompt"]


def test_l2_analyses_of_the_day_reach_the_prompt(db_session: Session) -> None:
    """The macro half of the join. Without it the model can group names but
    cannot say what the shared driver was."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")
    _seed_l2(db_session, analysis="Thirty-year yields hit 5.31% at auction. [Established]")

    captured: dict[str, str] = {}

    def _capture(*args: Any, **kwargs: Any) -> Any:
        captured["prompt"] = args[3] if len(args) > 3 else kwargs["user_prompt"]
        return json.dumps({"clusters": []})

    with (
        patch.object(l3, "_openrouter_client", return_value=MagicMock()),
        patch.object(l3, "_call_llm", side_effect=_capture),
    ):
        l3.get_day_synthesis(db_session, _DATE)

    assert "5.31%" in captured["prompt"]


def test_stale_prompt_version_rows_never_reach_the_synthesis(db_session: Session) -> None:
    """PR #167 review round 1, suggestion: `_load_day_briefings`/
    `_load_day_events` selected every non-null analysis for `trade_date` with
    NO `prompt_version` filter. `ticker_intel` is unique on `(identifier,
    trade_date, prompt_version)`, and a same-day deploy (or, as it happened,
    a stale `l1-v3` row still sitting in production alongside real `l1-v4`
    rows for the same identifier and date) means two rows for one identifier
    can coexist. Without the filter, L3 could hash and synthesize from a
    retired stub — exactly the "do not serve an older contract under a new
    one" rule the unique key encodes for L1/L2 themselves, violated one layer
    up by their consumer.
    """
    _seed_l1(db_session, "TSM", analysis="STALE stub text.", prompt_version="l1-v2")
    _seed_l1(db_session, "TSM", analysis="Current dated briefing text.")
    _seed_l1(db_session, "ASML")
    _seed_l2(
        db_session,
        event_key="theme:old",
        analysis="STALE macro stub.",
        prompt_version="l2-v0",
    )
    _seed_l2(db_session, analysis="Current macro briefing text.")

    captured: dict[str, str] = {}

    def _capture(*args: Any, **kwargs: Any) -> Any:
        captured["prompt"] = args[3] if len(args) > 3 else kwargs["user_prompt"]
        return json.dumps({"clusters": []})

    with (
        patch.object(l3, "_openrouter_client", return_value=MagicMock()),
        patch.object(l3, "_call_llm", side_effect=_capture),
    ):
        l3.get_day_synthesis(db_session, _DATE)

    assert "Current dated briefing text" in captured["prompt"]
    assert "STALE stub text" not in captured["prompt"]
    assert "Current macro briefing text" in captured["prompt"]
    assert "STALE macro stub" not in captured["prompt"]


# ---------------------------------------------------------------------------
# 3. Sharing: one inference per trade date
# ---------------------------------------------------------------------------


def test_second_call_same_day_same_inputs_hits_cache_no_llm_call(db_session: Session) -> None:
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    with _patched_llm({"clusters": [_cluster(["TSM", "ASML"])]}) as _client:
        first = l3.get_day_synthesis(db_session, _DATE)

    with patch.object(l3, "_call_llm", side_effect=AssertionError("must not call the LLM again")):
        second = l3.get_day_synthesis(db_session, _DATE)

    assert first == second
    assert len(first) == 1
    rows = db_session.execute(select(CrossNameIntel)).scalars().all()
    assert len(rows) == 1


def test_a_new_identifier_appearing_later_produces_a_fresh_synthesis(
    db_session: Session,
) -> None:
    """A later user in the same fan-out contributes identifiers the first
    user's synthesis never saw.

    Keying on `(trade_date, prompt_version)` ALONE would freeze the day's
    conclusion to whatever the first user's book happened to cover — the same
    "early write locks the day" failure round 6 found in L1's headline-only
    path (design doc §4.8, addendum 4), one layer up. The key therefore
    includes a fingerprint of the global input set, which is still a global
    key: it is derived from `ticker_intel` rows, never from who asked.
    """
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")
    with _patched_llm({"clusters": [_cluster(["TSM", "ASML"])]}):
        l3.get_day_synthesis(db_session, _DATE)

    _seed_l1(db_session, "SGOL")
    _seed_l1(db_session, "GLD")
    with _patched_llm({"clusters": [_cluster(["SGOL", "GLD"], mechanism="safe_haven")]}):
        second = l3.get_day_synthesis(db_session, _DATE)

    assert {c["mechanism"] for c in second} == {"safe_haven"}
    rows = db_session.execute(select(CrossNameIntel)).scalars().all()
    assert len(rows) == 2, "a changed global input set must not overwrite the earlier row"


def test_daily_cap_bounds_fresh_syntheses(db_session: Session) -> None:
    """Beyond the day's cap, a changed input set degrades to the last stored
    synthesis rather than buying another call."""
    for i in range(l3._MAX_SYNTHESES_PER_DAY):
        _seed_l1(db_session, f"NAME{i}A")
        _seed_l1(db_session, f"NAME{i}B")
        with _patched_llm({"clusters": [_cluster([f"NAME{i}A", f"NAME{i}B"])]}):
            l3.get_day_synthesis(db_session, _DATE)

    _seed_l1(db_session, "LATECOMER")
    with patch.object(l3, "_call_llm", side_effect=AssertionError("cap must stop the call")):
        result = l3.get_day_synthesis(db_session, _DATE)

    assert result, "over-cap must serve the most recent stored synthesis, not nothing"


def test_fewer_than_two_briefed_identifiers_never_calls_the_llm(db_session: Session) -> None:
    """One identifier cannot form a cross-name conclusion; asking is pure cost.

    Nothing is written either — not even an attempt marker (design doc §4.8
    addendum 4's rule): a later user in the same fan-out may well raise the
    day's identifier count past two, and a marker would lock that out."""
    _seed_l1(db_session, "TSM")

    with patch.object(l3, "_call_llm", side_effect=AssertionError("must not call the LLM")):
        assert l3.get_day_synthesis(db_session, _DATE) == []

    assert db_session.execute(select(CrossNameIntel)).scalars().all() == []


def test_null_analysis_l1_rows_are_not_offered_as_input(db_session: Session) -> None:
    """A null-analysis marker row means "attempted, nothing to serve" — it has
    no text to reason from, so it must not count toward the two-identifier
    floor either."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "BLOCKED", analysis=None)

    with patch.object(l3, "_call_llm", side_effect=AssertionError("must not call the LLM")):
        assert l3.get_day_synthesis(db_session, _DATE) == []


# ---------------------------------------------------------------------------
# 4. Output validation — closed enums and structural sanity
# ---------------------------------------------------------------------------


def test_identifier_not_briefed_today_is_dropped_from_a_cluster(db_session: Session) -> None:
    """The model may only group names that were actually supplied.

    An invented identifier would not error anywhere downstream — it would just
    intersect with nothing, or worse, print a ticker nobody analyzed into a
    report as though it had been. Same closed-set discipline A3 applies to
    asset classes."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")
    _seed_l1(db_session, "MUU")

    with _patched_llm({"clusters": [_cluster(["TSM", "ASML", "NVDA"])]}):
        clusters = l3.get_day_synthesis(db_session, _DATE)

    assert clusters[0]["identifiers"] == ["TSM", "ASML"]


def test_cluster_left_with_one_identifier_is_dropped(db_session: Session) -> None:
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    with _patched_llm({"clusters": [_cluster(["TSM", "NVDA"])]}):
        assert l3.get_day_synthesis(db_session, _DATE) == []


def test_out_of_taxonomy_mechanism_drops_the_cluster(db_session: Session) -> None:
    """The mechanism IS the conclusion, so an unrecognized one leaves nothing
    to say — unlike A3's asset-class list, where dropping one label still
    leaves a usable event."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    with _patched_llm({"clusters": [_cluster(["TSM", "ASML"], mechanism="vibes")]}):
        assert l3.get_day_synthesis(db_session, _DATE) == []


def test_missing_or_invalid_confidence_falls_back_to_the_weakest_label(
    db_session: Session,
) -> None:
    """A label typo must not cost the whole conclusion, but it must never be
    silently upgraded — [Speculative] is the conservative floor."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    with _patched_llm({"clusters": [_cluster(["TSM", "ASML"], confidence="very likely")]}):
        clusters = l3.get_day_synthesis(db_session, _DATE)

    assert clusters[0]["confidence"] == "Speculative"


def test_confidence_case_is_normalized_not_downgraded(db_session: Session) -> None:
    """PR #167 review round 1, nit: the closed-set check was case-sensitive,
    so a real, well-formed answer like "probable" (lowercase, a plausible
    model output despite the system prompt's exact casing) was silently
    treated as an unrecognized label and downgraded to Speculative — a
    conservative failure mode, not a leak, but a real conclusion mislabeled
    as a closed enum value it should have matched."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    with _patched_llm({"clusters": [_cluster(["TSM", "ASML"], confidence="probable")]}):
        clusters = l3.get_day_synthesis(db_session, _DATE)

    assert clusters[0]["confidence"] == "Probable"


def test_prose_wrapped_json_still_parses(db_session: Session) -> None:
    """Same second chance A3's `_parse_l2_response` gives: a model that
    prefaces its JSON with a sentence is a formatting habit, and the cost of
    treating it as failure is the whole day's cross-name conclusion."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    client = MagicMock()
    raw = "Here is the analysis:\n" + json.dumps({"clusters": [_cluster(["TSM", "ASML"])]})
    with (
        patch.object(l3, "_openrouter_client", return_value=client),
        patch.object(l3, "_call_llm", return_value=raw),
    ):
        clusters = l3.get_day_synthesis(db_session, _DATE)

    assert len(clusters) == 1


# ---------------------------------------------------------------------------
# 5. Compliance + failure handling (mirrors L1/L2)
# ---------------------------------------------------------------------------


def test_forbidden_output_is_not_cached_and_alerts(db_session: Session) -> None:
    """A blocked synthesis ships to nobody. Blast radius here is every user
    holding any name in the cluster, so the scan runs BEFORE the write."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    bad = _cluster(["TSM", "ASML"], summary="Investors should buy the dip here.")
    with patch.object(l3, "send_ops_alert") as alert, _patched_llm({"clusters": [bad]}):
        assert l3.get_day_synthesis(db_session, _DATE) == []

    assert alert.called
    row = db_session.execute(select(CrossNameIntel)).scalars().one()
    assert row.clusters is None, "forbidden text must never be stored"


def test_disclaimer_line_does_not_false_trip_the_scan(db_session: Session) -> None:
    """Round-7 lesson from L1 (design doc §4.8 addendum 6): strip markers
    BEFORE scanning, or a model-emitted disclaimer blacklists the day's only
    synthesis slot."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    summary = "Rates drove the group. [Probable]\n本简报不构成投资建议，仅供参考。"  # noqa: RUF001
    with _patched_llm({"clusters": [_cluster(["TSM", "ASML"], summary=summary)]}):
        clusters = l3.get_day_synthesis(db_session, _DATE)

    assert len(clusters) == 1


def test_retryable_failure_leaves_attempts_for_a_later_caller(db_session: Session) -> None:
    """Issue #160's bounded retry, same contract as L1/L2: a connection blip
    during the first user's report must not cost every later user the day's
    cross-name conclusion."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    boom = openai.APIConnectionError(request=httpx.Request("POST", "https://x.test"))
    with (
        patch.object(l3, "_openrouter_client", return_value=MagicMock()),
        patch.object(l3, "_call_llm", side_effect=boom),
    ):
        assert l3.get_day_synthesis(db_session, _DATE) == []

    row = db_session.execute(select(CrossNameIntel)).scalars().one()
    assert row.clusters is None
    assert row.attempt_count == 1

    with _patched_llm({"clusters": [_cluster(["TSM", "ASML"])]}):
        assert len(l3.get_day_synthesis(db_session, _DATE)) == 1


def test_non_retryable_failure_locks_the_key_immediately(db_session: Session) -> None:
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "ASML")

    boom = openai.AuthenticationError(
        "bad key",
        response=httpx.Response(401, request=httpx.Request("POST", "https://x.test")),
        body=None,
    )
    with (
        patch.object(l3, "_openrouter_client", return_value=MagicMock()),
        patch.object(l3, "_call_llm", side_effect=boom),
    ):
        assert l3.get_day_synthesis(db_session, _DATE) == []

    row = db_session.execute(select(CrossNameIntel)).scalars().one()
    assert row.attempt_count == l3._MAX_ATTEMPTS_PER_KEY

    with patch.object(l3, "_call_llm", side_effect=AssertionError("locked key must not retry")):
        assert l3.get_day_synthesis(db_session, _DATE) == []


# ---------------------------------------------------------------------------
# 6. The per-user filter — where a leak would become visible in a report
# ---------------------------------------------------------------------------


def test_clusters_for_user_narrows_to_the_users_own_l1_keys() -> None:
    clusters = [_cluster(["TSM", "ASML", "MUU"])]
    out = l3.clusters_for_user(clusters, ["TSM", "ASML"], ["TSM", "ASML", "MUU"])
    assert out[0]["identifiers"] == ["TSM", "ASML"]


def test_clusters_for_user_drops_a_cluster_with_fewer_than_two_of_the_users_names() -> None:
    """One name is not a cross-name conclusion, and printing "TSM belongs to a
    group" whose other members this user does not hold is both useless and
    disclosive."""
    assert l3.clusters_for_user([_cluster(["TSM", "ASML"])], ["TSM"], ["TSM", "ASML"]) == []


def test_cluster_summary_naming_an_unheld_identifier_is_dropped_for_that_user() -> None:
    """The structural guard behind the "summary names no identifiers" prompt
    rule.

    The identifier list is filterable; free text is not. If the model disobeys
    and writes a name into the summary, narrowing the list would still leave
    that name in the prose that reaches the report — a holding this user does
    not own, sourced from another user's book. Dropping the cluster is the
    conservative resolution: the cost is one missing sentence, the
    alternative is a leak.
    """
    clusters = [_cluster(["TSM", "ASML", "MUU"], summary="TSM, ASML and MUU rose together.")]
    assert l3.clusters_for_user(clusters, ["TSM", "ASML"], ["TSM", "ASML", "MUU"]) == []


def test_summary_naming_an_identifier_from_a_DIFFERENT_cluster_is_dropped() -> None:
    """PR #167 review round 1, bug 2, part 1: the first draft only built its
    denylist from THIS cluster's own filtered-out identifiers, so a leak
    routed through a name that belongs to a DIFFERENT cluster in the same
    day's set — never a member of this one — sailed through untouched.

    SGOL never appears in this cluster's `identifiers`, so under the old
    "excluded = this cluster's own filtered members" logic there was nothing
    to catch it. It is still a name this reader does not hold and the model
    saw in its prompt (it was briefed, and clustered elsewhere that day).
    """
    clusters = [
        _cluster(["TSM", "ASML"], summary="TSM and ASML moved with SGOL on the same driver."),
        _cluster(["SGOL", "GLD"], mechanism="safe_haven"),
    ]
    out = l3.clusters_for_user(clusters, ["TSM", "ASML"], ["TSM", "ASML", "SGOL", "GLD"])
    assert out == []


def test_summary_naming_a_briefed_but_never_clustered_identifier_is_dropped() -> None:
    """PR #167 review round 1, bug 2, part 1 (the stronger form the review
    calls out as the correct fix, not just the cluster-set union): the model
    is exposed to EVERY identifier briefed that day in its prompt, whether or
    not that identifier ended up assigned to any returned cluster. A leak
    naming one of those never-clustered identifiers has no cluster to be
    "from" at all — only the full day's briefing universe catches it."""
    clusters = [_cluster(["TSM", "ASML"], summary="TSM and ASML moved with MUU's memory read.")]
    # MUU was briefed that day but the model chose not to group it into any
    # cluster — it appears only in `all_briefed_identifiers`, nowhere in
    # `clusters` itself.
    out = l3.clusters_for_user(clusters, ["TSM", "ASML"], ["TSM", "ASML", "MUU"])
    assert out == []


def test_denylist_catches_a_configured_alias_not_just_the_ticker() -> None:
    """PR #167 review round 1, bug 2, part 2: `_mentions` only ever checked
    the raw ticker token. A model naming an excluded identifier by its
    company name — the exact vocabulary `holding_news_keywords.yml` already
    maps for this identifier — passed straight through."""
    clusters = [_cluster(["TSM", "ASML"], summary="TSM and ASML tracked Micron's read higher.")]
    out = l3.clusters_for_user(clusters, ["TSM", "ASML"], ["TSM", "ASML", "MUU"])
    assert out == []


def test_denylist_does_not_over_drop_on_theme_vocabulary_a_summary_may_legitimately_use() -> None:
    """PR #167 review round 2, suggestion: round 1's fix dumped EVERY
    `holding_news_keywords.yml` alias for an excluded identifier into the
    denylist, but that YAML is a news-RECALL table, not a "this text names
    that holding" synonym list — it mixes real entity names ("Micron",
    "TSMC") with theme/technology tokens a name-free mechanism summary is
    SUPPOSED to use ("lithography" for ASML, "gold"/"bullion" for three
    different gold funds, "Nasdaq" for QQQM, "HBM" for MUU). Under the
    unfixed logic, a summary about a completely different cluster (here:
    TSM + MUU, both held) that happens to use "lithography" or "gold" as
    ordinary mechanism vocabulary would be dropped for ANY reader missing
    ASML or SGOL that day — silently deleting the exact cross-name sentence
    this layer exists to produce, on the semiconductor/haven clusters the
    whole PR was built around.

    ASML and SGOL are briefed that day (in `all_briefed_identifiers`) but
    this reader holds neither — they are the two excluded identifiers whose
    THEME aliases must NOT reach the denylist. The summary is genuinely
    name-free: it never says "ASML" or "SGOL", only their theme vocabulary.
    """
    clusters = [
        _cluster(
            ["TSM", "MUU"],
            summary=(
                "TSM and MUU rose together on lithography-driven capex demand "
                "and a broader haven bid in gold."
            ),
        )
    ]
    out = l3.clusters_for_user(clusters, ["TSM", "MUU"], ["TSM", "MUU", "ASML", "SGOL"])
    assert len(out) == 1
    assert out[0]["identifiers"] == ["TSM", "MUU"]


def test_denylist_catches_the_unsuffixed_stem_of_an_excluded_identifier() -> None:
    """PR #167 review round 1, bug 2, part 2: "513650" (no ".SS" suffix)
    naming the excluded "513650.SS" bypassed the raw-ticker-only check."""
    clusters = [_cluster(["TSM", "ASML"], summary="TSM and ASML moved alongside 513650 today.")]
    out = l3.clusters_for_user(clusters, ["TSM", "ASML"], ["TSM", "ASML", "513650.SS"])
    assert out == []


def test_summary_mentioning_only_held_names_survives() -> None:
    clusters = [_cluster(["TSM", "ASML"], summary="TSM and ASML rose together.")]
    assert len(l3.clusters_for_user(clusters, ["TSM", "ASML"], ["TSM", "ASML"])) == 1


def test_clusters_for_user_is_a_pure_function_with_no_session(db_session: Session) -> None:
    """No DB handle means it cannot widen its own input to "everything cached
    today" — the same argument `report_assembly` makes for taking no Session."""
    params = set(inspect.signature(l3.clusters_for_user).parameters)
    assert "session" not in params


def test_day_briefed_identifiers_is_a_pure_read_no_narrowing(db_session: Session) -> None:
    """`day_briefed_identifiers` is the source `clusters_for_user`'s denylist
    is built from — it must return every identifier with a servable L1
    briefing that day, not just ones that ended up in a cluster."""
    _seed_l1(db_session, "TSM")
    _seed_l1(db_session, "MUU", analysis=None)  # attempted marker, not servable
    result = l3.day_briefed_identifiers(db_session, _DATE)
    assert result == ["TSM"]
