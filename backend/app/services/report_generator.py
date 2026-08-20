"""LLM report generation pipeline (Ring 0 — Stage F2).

Two-pass design:
  Pass 1  macro signals + headlines → LOW_COST_LLM → search queries
  Tavily  execute queries, collect background snippets
  Pass 2  portfolio snapshot + Pass 1 context + anomalies + search results → PRIMARY_LLM → §2/§3/§4 body
  Strip   remove any inline citations / provenance tags / per-line disclaimers
  Compliance scan  reject forbidden advisory language in the body (→ needs_review)
  Assemble  header + data-window + §1 (code-built) + cleaned §2/§3/§4 + footer
  Render   translate the assembled report to the output language (#8)
  Write   reports table (report_md + report_inputs JSONB)

Layer-3/4 compliance:
  - System prompt contains the full forbidden-vocabulary list and Layer 3 rule.
  - A post-generation scan backstops the prompt: a body that emits forbidden
    advisory language is held as 'needs_review' and never emailed.
  - The single disclaimer lives in the template footer (F3); the body carries no
    per-sentence disclaimer suffix and no bracketed provenance tags. The model is
    told not to emit them and `_strip_markers` removes any that slip through.
  - Holdings data is isolated to Pass 2; Pass 1 sees macro signals and public
    headlines only — never anomalies (which are holdings-derived).
  - OPENROUTER_DATA_COLLECTION = "deny" is enforced on every LLM call.

Orchestration only (#37): prompt text, code-built section renderers, the LLM
transport, Tavily search, serialization, and the compliance/translation
backstops each live in their own module — see report_prompts.py,
report_sections.py, report_llm.py, report_search.py, report_serializers.py,
app/compliance/output_scan.py, and report_translation.py. This file wires
them together into generate_report()/regenerate_report().
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.output_scan import (
    _scan_forbidden_output,
    _strip_body_disclaimer,
    _strip_markers,
)
from app.core.config import get_settings
from app.core.deps import get_current_user_id
from app.core.ops_log import log_ops_event
from app.core.timezones import ET
from app.models.report import Report
from app.services.cross_name_intel import (
    clusters_for_user,
    day_briefed_identifiers,
    get_day_synthesis,
)
from app.services.email_sender import send_ops_alert, send_report_email
from app.services.forward_events import FORWARD_WINDOW_DAYS, load_forward_events
from app.services.github_issues import create_bug_report
from app.services.holding_news import recall_holding_news
from app.services.macro_detector import detect_macro_signals
from app.services.macro_event_intel import (
    build_l2_facts,
    get_l2_intel_batch,
    l2_event_keys_for_user,
    user_event_exposure,
)
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import compute_portfolio
from app.services.price_anomaly_detector import PriceAnomaly
from app.services.report_assembly import (
    ASSEMBLY_PROMPT_VERSION,
    build_assembly_prompt,
    parse_shadow_models,
    portfolio_identifiers,
    run_assembly_pass,
    should_use_assembly,
)
from app.services.report_context import ReportContext, ReportInputsDict
from app.services.report_llm import _BYOK_PROVIDER_ORDER, _call_llm, _openrouter_client
from app.services.report_prompts import (
    _COMPLIANCE_SYSTEM_PREFIX,
    _PASS2_SYSTEM,
    _build_pass1_prompt,
    _build_pass2_prompt,
    body_is_incomplete,
)
from app.services.report_search import (
    _MAX_SEARCH_QUERIES,
    _run_tavily_search,
    _targeted_anomaly_queries,
    _tavily_used_today,
)
from app.services.report_sections import (
    _build_data_window,
    _build_footer,
    _build_forward_block,
    _build_section1,
    _build_section42_table,
    _build_section44_technical,
    _build_today_events_block,
    _fx_is_stale,
    _header_timestamp,
    _inject_forward_block,
    _inject_section42_table,
    _inject_today_events,
)
from app.services.report_serializers import (
    _serialize_anomalies,
    _serialize_macro,
    _serialize_news,
    _serialize_portfolio,
    _serialize_technical,
)
from app.services.report_translation import _translate_md
from app.services.report_types import validate_report_type
from app.services.shared_budget import fair_share_budget
from app.services.technical_position import compute_technical_positions
from app.services.ticker_intel import (
    build_l1_facts,
    get_l1_intel_batch,
    l1_identifiers_for_user,
    large_weight_identifiers,
)
from app.services.window_data import (
    L1_LOOKBACK_TRADING_DAYS,
    HoldingMove,
    MovesCache,
    day_window_bounds,
    detect_window_anomalies,
    latest_window_close_date,
    load_day_news,
    load_news_window,
    lookback_trading_dates,
    mark_news_surfaced,
    resolve_global_moves,
    unmark_news_surfaced,
    user_watermark,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPT_VERSION = "f2-v6"  # f2-v6: §4.2 cross-reference restricted to holdings actually in the anomaly table (R-8) + HOLDING-RELEVANT NEWS block from per-holding recall/targeted search (R-3); f2-v5 = direction-requires-evidence + divergence-is-the-signal (no price-direction claims without window data); f2-v4 = §4.2 code table + driver-only, evidence confidence labels, §4.4 technical position
_DISCLAIMER_VERSION = "f3-bilingual-v2"

# L1 leftover-budget top-up (issue #128 quality gate, design doc §6.7 item 3).
# An L1 candidate that moved this much with NOTHING recalled is the case that
# is guaranteed to come back [Speculative]; below it, an unexplained move is
# ordinary noise and not worth a scarce shared search. Two per report keeps the
# spend bounded when a whole sleeve moves at once — the daily Tavily budget is
# shared across the fan-out, so this must stay a top-up, never a sweep.
_L1_SEARCH_MIN_MOVE = 0.03
_MAX_L1_TOPUP_SEARCHES = 2

# Weight-driven material for Pass 2 itself, not just L1 (issue #128
# narrative-layer redesign, 2026-08-20 — design doc "第一步"). Anomaly-only
# material-gathering left large no-anomaly holdings (TSM at 22.5%, +1.22% on
# the 2026-08-17 anchor report) with zero recalled news and zero targeted
# search in Pass 2's OWN prompt — not just L1's shared cache, which already
# had a weight channel. Capped at the same top-K L1 already uses for its own
# weight channel (`ticker_intel._L1_TOP_K_BY_WEIGHT`): a holding large enough
# to need material without an anomaly is, by definition, a small set per
# report.
_MAX_WEIGHT_TARGETED_SEARCHES = 5

# Forward calendar (#1): how far ahead §2.5 looks — now defined once in
# forward_events.py, since the L2 shared cache (issue #128 A3) must analyze
# exactly the horizon this section renders (round-1 review nit, PR #157: two
# copies of the same 10 could drift into two different horizons).


# ---------------------------------------------------------------------------
# Assembly / rendering (shared by live generation and re-render)
# ---------------------------------------------------------------------------


def _render_full_md(
    report_date_str: str,
    portfolio: dict[str, Any],
    news_items: list[dict[str, Any]],
    raw_body: str,
    output_lang: str,
    period_start: str = "",
    period_end: str = "",
    trading_days: int = 0,
    anomalies: list[dict[str, Any]] | None = None,
    technical: list[dict[str, Any]] | None = None,
    forward_events: list[dict[str, Any]] | None = None,
    price_data_through: str = "",
) -> tuple[str, list[str], str]:
    """Annotate, assemble, language-render, and compliance-scan a report.

    Returns (full_markdown, violations, translated_body). The third element is
    the translated dynamic section (pre-footer) for compliance audit traceability.
    Pure function of its inputs — this is what makes #6 re-render possible.
    """
    cleaned = _strip_markers(raw_body)
    # §2.5 forward calendar is code-built from stored events + holdings (#1) and
    # inserted before §3: calendar facts mapped to exposed holdings, no forecast.
    if forward_events:
        cleaned = _inject_forward_block(
            cleaned,
            _build_forward_block(
                forward_events, portfolio.get("holdings", []), news_items, report_date_str
            ),
        )
        # R-6: events scheduled for the report's own date are no longer "forward"
        # — promote them to a "today" note at the top of §2 (calendar fact only,
        # results not yet in this report's data).
        today_block = _build_today_events_block(
            forward_events, portfolio.get("holdings", []), report_date_str
        )
        if today_block:
            cleaned = _inject_today_events(cleaned, today_block)
    # §4.2 numeric table is code-built from the stored anomalies and inserted
    # under the LLM's §4.2 heading (#3): deterministic, token-free, no hallucination.
    if anomalies:
        cleaned = _inject_section42_table(cleaned, _build_section42_table(anomalies))
    # §4.4 technical position appended to the §4 body (#4) — also code-built from
    # stored metrics, so re-render reproduces it without touching the DB.
    if technical:
        cleaned = cleaned.rstrip() + "\n\n" + _build_section44_technical(technical)
    header = f"# Portfonia Financial Analysis Report — {_header_timestamp(report_date_str, period_end)}\n\n"
    window = _build_data_window(
        news_items, portfolio, period_start, period_end, trading_days, price_data_through
    )
    section1 = _build_section1(portfolio)
    dynamic_en = header + window + section1 + "\n\n" + cleaned

    # Compliance scan on the English canonical first (highest-signal blacklist).
    violations = _scan_forbidden_output(cleaned)

    dynamic_out = _translate_md(dynamic_en, output_lang)
    # The translator can re-add its own disclaimer paragraph (it runs after the
    # pre-translation strip); remove it so the body carries no disclaimer and the
    # scan does not false-trip on its advisory-sounding wording in either language.
    dynamic_out = _strip_body_disclaimer(dynamic_out)
    if output_lang != "en":
        # Translation can paraphrase into advisory tone — re-scan the output.
        violations = violations + _scan_forbidden_output(dynamic_out)

    full_md = dynamic_out + _build_footer(portfolio)
    return full_md, violations, dynamic_out


# ---------------------------------------------------------------------------
# A4 personalized assembly (issue #128, design doc §6)
# ---------------------------------------------------------------------------


def _assembly_prompt_from_ctx(ctx: ReportContext) -> str:
    """Build the assembly prompt from THIS report's context and nothing else.

    Every argument is a `ctx` field already scoped to one user — the shared
    caches are read through `ctx.ticker_intel`/`ctx.macro_event_intel`, which
    `l1_identifiers_for_user`/`l2_event_keys_for_user` narrowed to this user's
    own candidates. `build_assembly_prompt` takes no Session precisely so this
    stays the only way in (see report_assembly.py's docstring).
    """
    return build_assembly_prompt(
        ctx.portfolio_summary,
        ctx.price_anomalies,
        ctx.ticker_intel,
        ctx.macro_event_intel,
        ctx.macro_event_exposure,
        ctx.period_start,
        ctx.period_end,
        ctx.window_trading_days,
        ctx.technical_positions,
        ctx.cross_name_intel,
    )


def _try_assembly(
    client: Any, settings: Any, ctx: ReportContext, report_id: uuid.UUID
) -> str | None:
    """The assembled body, or None meaning "fall back to Pass 2".

    Every failure mode returns None rather than raising: a switch that is
    off, a model not yet chosen, empty shared caches, a provider error, or a
    truncated body. That is the design doc §6.3 guarantee — the worst case of
    enabling A4 is the pre-A4 report, never a thinner one. The cost of a
    fallback is one wasted assembly call, which is the cheap half of the
    pair.
    """
    if not should_use_assembly(
        enabled=bool(settings.SHARED_COMPUTE_ENABLED),
        model=str(settings.ASSEMBLY_LLM_MODEL),
        ticker_intel=ctx.ticker_intel,
        macro_event_intel=ctx.macro_event_intel,
    ):
        return None

    model = str(settings.ASSEMBLY_LLM_MODEL).strip()
    prompt = _assembly_prompt_from_ctx(ctx)
    logger.info("report %s: assembly pass (%s)", report_id, model)
    try:
        body = run_assembly_pass(client, model, prompt, usage_sink=ctx.llm_calls)
    except Exception:
        logger.exception("report %s: assembly pass failed — falling back to Pass 2", report_id)
        return None

    if body_is_incomplete(body):
        logger.warning(
            "report %s: assembled body looks truncated (%d chars) — falling back to Pass 2",
            report_id,
            len(body),
        )
        return None

    ctx.body_source = "assembly"
    ctx.assembly_model = model
    ctx.assembly_prompt = prompt
    ctx.assembly_raw = body
    ctx.assembly_prompt_version = ASSEMBLY_PROMPT_VERSION
    return body


def _run_shadow_assembly(
    client: Any, settings: Any, ctx: ReportContext, report_id: uuid.UUID
) -> None:
    """Run the assembly pass once per shadow model, store, ship nothing.

    Design doc §6.3.1: one round produces BOTH comparisons — architecture
    (the shipped body vs each assembled body) and model selection (the listed
    models against each other) — over an identical prompt, with costs landing
    in the same `ctx.llm_calls` the shipped passes use.

    Wrapped so a shadow failure is recorded and moved past: a measurement
    harness must never be able to fail the thing it measures.
    """
    models = parse_shadow_models(str(settings.ASSEMBLY_SHADOW_MODELS))
    if not models:
        return
    if not ctx.ticker_intel and not ctx.macro_event_intel:
        logger.info("report %s: no shared intel this run — skipping shadow assembly", report_id)
        return

    prompt = _assembly_prompt_from_ctx(ctx)
    for model in models:
        entry: dict[str, Any] = {"prompt_version": ASSEMBLY_PROMPT_VERSION}
        if ctx.body_source == "assembly" and model == ctx.assembly_model:
            # Already ran as the shipped body — re-running would bill twice
            # for identical output. Point at it so the side-by-side read is
            # still complete.
            entry["raw"] = ctx.assembly_raw
            entry["shipped"] = True
            ctx.assembly_shadow[model] = entry
            continue
        logger.info("report %s: shadow assembly pass (%s)", report_id, model)
        try:
            entry["raw"] = run_assembly_pass(client, model, prompt, usage_sink=ctx.llm_calls)
        except Exception as exc:
            logger.exception("report %s: shadow assembly failed (%s)", report_id, model)
            entry["error"] = f"{type(exc).__name__}: {exc}"
        ctx.assembly_shadow[model] = entry


# A manual window this short (hours) with nothing in it is a same-day re-run
# artifact, not a real reporting period. (R-7)
_SHORT_MANUAL_WINDOW_HOURS = 2.0


def _is_short_manual_quiet(
    session_node: str,
    period_start: datetime,
    period_end: datetime,
    news_items: list[NewsItem],
    anomalies: list[PriceAnomaly],
) -> bool:
    """True for a manual re-run over a tiny, empty window (R-7).

    All four must hold: triggered manually, window under the short threshold,
    no window news, no anomalies. Scheduled triggers (after_close) never match,
    so a genuinely quiet scheduled week still sends its heartbeat.
    """
    if session_node != "manual" or news_items or anomalies:
        return False
    span_hours = (period_end - period_start).total_seconds() / 3600.0
    return span_hours < _SHORT_MANUAL_WINDOW_HOURS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_report(
    session: Session,
    report_date: date | None = None,
    report_type: str = "incremental",
    base_currency: str = "USD",
    output_lang: str = "en",
    session_node: str = "manual",
    user_id: uuid.UUID | None = None,
    moves_cache: MovesCache | None = None,
    now: datetime | None = None,
    users_remaining: int = 1,
) -> Report:
    """
    Run the full F1 report generation pipeline and persist the result.

    Returns the Report ORM object (status='success' or 'failed').
    Raises if the report record cannot be written (e.g. unique constraint violation
    when a report for the same date+type+session_node already exists).

    `user_id` (issue #128 A1): `None` falls back to `get_current_user_id()`
    (Ring 0's fixed dev user), preserving every existing single-user call
    site unchanged. `generate_incremental_report`'s multi-user fan-out passes
    the actual user being generated for.

    `moves_cache` (issue #128 A1): forwarded to `detect_window_anomalies` —
    see its docstring in `window_data.py`. Lets a multi-user batch share one
    `compute_global_moves()` call across every user in the same window
    instead of recomputing it once per user. `None` (every existing call
    site) preserves the pre-A1 per-call behavior.

    `now` (issue #128 A1, PR #151 review): the wall-clock instant used for
    BOTH `eff_date`'s fallback and a fresh row's `period_end`. `None`
    (every pre-A1 call site) reads the real clock, unchanged. This exists
    because `moves_cache` is keyed on the exact `(period_start, period_end)`
    tuple — if each user's `generate_report` call in a fan-out stamped its
    own independent `datetime.now()`, two users sharing a window would get
    `period_end` values microseconds apart, the cache key would never
    collide, and `compute_global_moves` would silently run once per user
    again despite `moves_cache` being passed. `generate_incremental_report`
    stamps ONE `now` for the whole batch and passes it to every user's call
    so the cache key is actually shared, not just the dict object.

    `users_remaining` (issue #128 A4): how many users, INCLUDING this one,
    the current fan-out still has to serve. Forwarded to the L1/L2 shared
    caches so each user gets a fair slice of the day's remaining analysis
    budget instead of the first user in a never-rotating order spending it
    all — see `shared_budget.fair_share_budget` for why this same problem
    surfaced once per checkpoint. `1` (every non-fan-out call site: manual
    trigger, tests, a single-user system) means no restriction at all.
    """
    validate_report_type(report_type)
    settings = get_settings()
    user_id = user_id if user_id is not None else get_current_user_id()
    # A local cache when the caller supplied none: the global move set has two
    # consumers in this function (anomaly detection, then L1's shared-intel
    # facts — see §5.5), and without a cache to share, the second would pay
    # for a full second `compute_global_moves()` on every single-user call.
    moves_cache = moves_cache if moves_cache is not None else {}
    now = now if now is not None else datetime.now(tz=UTC)
    eff_date = report_date or now.astimezone(ET).date()

    # ------------------------------------------------------------------
    # Idempotency: (user_id, report_date, report_type, session_node) is unique
    # (H-DEBT-1). session_node identifies WHICH trigger produced the report
    # (e.g. "manual" vs "after_close" for the M/W/F 17:00 ET cadence) so two
    # distinct triggers on the same calendar day get separate rows / windows /
    # emails. A redelivered Celery task (task_acks_late=True) or a repeated
    # manual /reports/generate passes the SAME session_node, so it still
    # short-circuits a completed report instead of inserting a duplicate; reuse
    # a prior failed/in_progress row so a retry can regenerate in place.
    # ------------------------------------------------------------------
    existing = session.execute(
        select(Report).where(
            Report.user_id == user_id,
            Report.report_date == eff_date,
            Report.report_type == report_type,
            Report.session_node == session_node,
        )
    ).scalar_one_or_none()

    if existing is not None and existing.status in ("success", "skipped"):
        logger.info(
            "report %s: already complete for %s (status=%s) — returning existing",
            existing.id,
            eff_date,
            existing.status,
        )
        return existing

    # ------------------------------------------------------------------
    # Create or reset report record (status=in_progress)
    # ------------------------------------------------------------------
    if existing is not None:
        report = existing
        report.status = "in_progress"
        report.prompt_version = _PROMPT_VERSION
        report.disclaimer_version = _DISCLAIMER_VERSION
        report.report_md = None
        report.report_inputs = None
        report.generated_at = None
        report.email_sent_at = None
        # issue #45 review follow-up: email_sent_at and provider_message_id are
        # a pair (both set together in email_sender.send_report_email). Clearing
        # only email_sent_at here would leave a stale Resend id from the prior
        # send attached to a row that now reads as "not sent".
        report.provider_message_id = None
        # H-DEBT-3 / PR #139 review: this row's window is frozen and reused
        # below, so a retry (e.g. a reopened needs_review row) must reselect
        # the SAME news candidate set the first attempt saw. Without this, a
        # prior attempt's own mark_news_surfaced call would make
        # load_news_window silently exclude those items on retry — a no-op
        # for a failed row (never marked), a real bug for needs_review.
        unmark_news_surfaced(session, report.id)
    else:
        report = Report(
            user_id=user_id,
            report_date=eff_date,
            report_type=report_type,
            session_node=session_node,
            status="in_progress",
            prompt_version=_PROMPT_VERSION,
            disclaimer_version=_DISCLAIMER_VERSION,
        )
        session.add(report)
    # ADR-002 incremental window: [previous report's period_end, now], computed
    # ONCE on the first attempt and then frozen for the lifetime of this row. A
    # retry of a failed/needs_review row reuses the original window rather than
    # recomputing it: recomputing on every retry made the window (and therefore
    # the report content) non-deterministic across retries of the SAME row,
    # which is both a bad dedup invariant (two attempts at "the same report"
    # produce different content) and the path by which a same-day retry could
    # collapse start_date == end_date.
    if report.period_start is None or report.period_end is None:
        # Exclude this row from the watermark: its own (not-yet-committed)
        # period_end must not become its own period_start — autoflush=False
        # means the status reset above is not yet visible to this query anyway,
        # but a brand-new row also has no period_end yet to read back.
        period_start = user_watermark(
            session,
            user_id,
            report_type,
            exclude_report_id=report.id if existing is not None else None,
        )
        period_end = now
        report.period_start = period_start
        report.period_end = period_end
        session.flush()  # get the id without committing
        logger.info(
            "report %s: generation started for %s (window %s → %s)",
            report.id,
            eff_date,
            period_start.isoformat(),
            period_end.isoformat(),
        )
    else:
        assert report.period_start is not None and report.period_end is not None
        period_start = report.period_start
        period_end = report.period_end
        logger.info(
            "report %s: retrying for %s (window frozen at %s → %s)",
            report.id,
            eff_date,
            period_start.isoformat(),
            period_end.isoformat(),
        )

    log_ops_event(
        "report.generate.start",
        report_id=str(report.id),
        report_date=str(eff_date),
        session_node=session_node,
        report_type=report_type,
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
    )

    ctx = ReportContext()
    ctx.period_start = period_start.isoformat()
    ctx.period_end = period_end.isoformat()

    try:
        # ------------------------------------------------------------------
        # 1. Gather inputs (news + price moves read from the capture stores)
        # ------------------------------------------------------------------
        logger.info("report %s: fetching portfolio snapshot", report.id)
        portfolio_snap = compute_portfolio(
            session,
            user_id=user_id,
            base_currency=base_currency,
            as_of=period_end.astimezone(ET).date(),
        )
        ctx.portfolio_summary = _serialize_portfolio(portfolio_snap)
        if portfolio_snap.stale_tickers:
            stale_list = ", ".join(portfolio_snap.stale_tickers)
            logger.warning(
                "report %s: %d holding(s) missing price, excluded from report: %s",
                report.id,
                len(portfolio_snap.stale_tickers),
                stale_list,
            )
            alert_body = (
                f"Report {report.id} ({report.report_date}) excluded the following "
                f"holdings due to missing price data:\n\n"
                + "\n".join(f"  - {t}" for t in portfolio_snap.stale_tickers)
                + "\n\nCheck price_snapshots and capture logs."
            )
            send_ops_alert(
                subject=f"[Portfonia] price missing — {len(portfolio_snap.stale_tickers)} holding(s) excluded",
                body=alert_body,
                idempotency_key=f"ops-price-missing-{report.id}",
            )
            create_bug_report(
                title=f"holdings excluded: price missing for {stale_list}",
                body=(
                    f"## Holdings excluded from report due to missing price\n\n"
                    f"**Report:** {report.id} ({report.report_date})\n\n"
                    f"**Excluded holdings:** {stale_list}\n\n"
                    f"These holdings were absent from §1 portfolio composition and all "
                    f"aggregation totals. Likely causes: capture task failure, new holding "
                    f"with no price_snapshots row, or ticker/fund_code lookup mismatch.\n\n"
                    f"**Fix:** verify `price_snapshots` has recent rows for each identifier "
                    f"and that `compute_portfolio` looks them up correctly."
                ),
                labels=["bug", "ops", "data-quality"],
            )

        if portfolio_snap.stale_priced_tickers:
            stale_priced_list = ", ".join(portfolio_snap.stale_priced_tickers)
            logger.warning(
                "report %s: %d holding(s) have stale price data (>4 days): %s",
                report.id,
                len(portfolio_snap.stale_priced_tickers),
                stale_priced_list,
            )
            send_ops_alert(
                subject=f"[Portfonia] price data stale — {len(portfolio_snap.stale_priced_tickers)} holding(s)",
                body=(
                    f"Report {report.id} ({report.report_date}) used price data older than "
                    f"4 calendar days for the following holdings:\n\n"
                    + "\n".join(f"  - {t}" for t in portfolio_snap.stale_priced_tickers)
                    + "\n\nHoldings are included in totals but valuations may not reflect "
                    "recent market moves.\n\nCheck price capture logs for these tickers."
                ),
                idempotency_key=f"ops-price-stale-{report.id}",
            )

        # FX stale check: if rates trail the window cutoff, valuation in non-USD
        # currencies is based on stale exchange rates — alert ops but don't block.
        fx_date_str = ctx.portfolio_summary.get("fx_date", "")
        if fx_date_str and _fx_is_stale(fx_date_str, period_end.isoformat()):
            send_ops_alert(
                subject=f"[Portfonia] FX rates stale — report {report.report_date}",
                body=(
                    f"Report {report.id} ({report.report_date}): FX rates are as of "
                    f"{fx_date_str}, which trails the window cutoff. "
                    f"CNY/HKD portfolio values and the FX footer note will reflect "
                    f"stale exchange rates.\n\n"
                    f"Likely cause: capture_fx_task missed or failed. "
                    f"Check worker.log and run capture_fx_task.apply() to backfill."
                ),
                idempotency_key=f"ops-fx-stale-{report.id}",
            )

        logger.info("report %s: loading windowed news", report.id)
        news_items = load_news_window(session, period_start, period_end, user_id)
        ctx.news_items = _serialize_news(news_items)

        logger.info("report %s: detecting macro signals", report.id)
        macro_signals = detect_macro_signals(news_items)
        ctx.macro_signals = _serialize_macro(macro_signals)

        logger.info("report %s: detecting windowed price anomalies", report.id)
        anomalies, trading_days = detect_window_anomalies(
            session, period_start, period_end, user_id, moves_cache
        )
        ctx.price_anomalies = _serialize_anomalies(anomalies)
        ctx.window_trading_days = trading_days

        # R-5: the real price-data cutoff (last in-window close), distinct from
        # period_end. Stored so the data-window line and re-render agree.
        price_through = latest_window_close_date(session, period_start, period_end)
        ctx.price_data_through = price_through.isoformat() if price_through else ""

        # Technical position (#4): computed here (needs the session) and stored, so
        # §4.4 stays code-built and re-render reproduces it without a DB read.
        logger.info("report %s: computing technical position", report.id)
        ctx.technical_positions = _serialize_technical(
            compute_technical_positions(
                session, ctx.portfolio_summary.get("holdings", []), eff_date
            )
        )

        # Forward calendar (#1): read the scheduled US events the capture task
        # persisted. Stored so re-render reproduces §2.5 without a DB read.
        logger.info("report %s: loading forward calendar", report.id)
        ctx.forward_events = load_forward_events(
            session, eff_date, eff_date + timedelta(days=FORWARD_WINDOW_DAYS)
        )

        # ------------------------------------------------------------------
        # 2. Skip check
        # ------------------------------------------------------------------
        if not macro_signals.has_any_hit and not anomalies:
            logger.info("report %s: quiet day — no signals, no anomalies", report.id)
            quiet_body = (
                "## §2 Macro Signals\n\n"
                "No macro keyword themes triggered in this report period.\n\n"
                "## §3 Holdings Analysis\n\n"
                "No significant market developments detected for monitored holdings.\n\n"
                "## §4 Risk Radar\n\n"
                "No price anomalies or concentration alerts in this report period."
            )
            quiet_md, _, _ = _render_full_md(
                eff_date.strftime("%Y-%m-%d"),
                ctx.portfolio_summary,
                ctx.news_items,
                quiet_body,
                output_lang,
                ctx.period_start,
                ctx.period_end,
                ctx.window_trading_days,
                price_data_through=ctx.price_data_through,
            )
            report.status = "skipped"
            report.report_md = quiet_md
            report.report_inputs = ctx.to_jsonb()
            report.generated_at = datetime.now(tz=UTC)
            # H-DEBT-3 (#30): mark this window's news as surfaced in the same
            # transaction as the status commit, so the two can never diverge.
            mark_news_surfaced(session, user_id, report.id, [item.url_hash for item in news_items])
            session.commit()
            log_ops_event("report.generate.end", report_id=str(report.id), status="skipped")
            # R-7: a short manual re-run (e.g. a same-day second trigger minutes
            # after the first) covers a near-empty window — 0 news, 0 anomalies,
            # nothing the first report didn't have. Emailing it is pure noise
            # (the scheduled cadence never produces this). Skip the heartbeat for
            # that case only; a genuinely quiet SCHEDULED window still emails so
            # a calm week is distinguishable from a broken pipeline.
            if _is_short_manual_quiet(
                session_node, period_start, period_end, news_items, anomalies
            ):
                logger.info(
                    "report %s: short manual quiet window — suppressing heartbeat email",
                    report.id,
                )
                return report
            try:
                if not send_report_email(report, session):
                    logger.warning(
                        "report %s: email sent but state unconfirmed (commit failed)",
                        report.id,
                    )
            except Exception:
                logger.exception("report %s: quiet-day email send raised unexpectedly", report.id)
            return report

        # ------------------------------------------------------------------
        # 3. Pass 1 — search intent
        # ------------------------------------------------------------------
        client = _openrouter_client()
        low_cost_model = settings.LOW_COST_LLM_MODEL

        pass1_system = _COMPLIANCE_SYSTEM_PREFIX + (
            "\nYou are generating search queries for a financial intelligence analyst. "
            "Output ONLY a JSON object with a list of search queries. No other text."
        )
        # Anomalies are intentionally NOT passed: they are holdings-derived and
        # Pass 1 must stay holdings-free (see _build_pass1_prompt).
        pass1_user = _build_pass1_prompt(macro_signals, news_items)

        ctx.pass1_model = low_cost_model
        ctx.pass1_prompt = pass1_user

        logger.info("report %s: Pass 1 LLM call (%s)", report.id, low_cost_model)
        raw_pass1 = _call_llm(
            client,
            low_cost_model,
            pass1_system,
            pass1_user,
            with_holdings=False,
            pin_provider=False,
            provider_order=_BYOK_PROVIDER_ORDER,
            allow_fallbacks=False,
            enforce_data_collection=False,
            disable_reasoning=True,
            usage_sink=ctx.llm_calls,
        )
        ctx.pass1_raw = raw_pass1

        # Parse search queries from Pass 1 response
        search_queries: list[str] = []
        try:
            # Strip possible markdown fences
            clean = raw_pass1.strip()
            if clean.startswith("```"):
                clean = "\n".join(
                    ln for ln in clean.splitlines() if not ln.strip().startswith("```")
                ).strip()
            parsed = json.loads(clean)
            search_queries = [str(q) for q in parsed.get("queries", []) if q]
        except Exception:
            logger.warning("report %s: could not parse Pass 1 JSON, using empty queries", report.id)
        ctx.search_queries = search_queries[:_MAX_SEARCH_QUERIES]

        # ------------------------------------------------------------------
        # 4. Tavily search  — daily budget enforced across runs
        # ------------------------------------------------------------------
        used_today = _tavily_used_today(session, eff_date)
        daily_remaining = max(0, settings.TAVILY_DAILY_BUDGET - used_today)
        if ctx.search_queries:
            logger.info(
                "report %s: running %d Tavily queries (daily budget %d, used today %d, remaining %d)",
                report.id,
                len(ctx.search_queries),
                settings.TAVILY_DAILY_BUDGET,
                used_today,
                daily_remaining,
            )
            search_results = _run_tavily_search(
                session, ctx.search_queries, eff_date, budget=daily_remaining
            )
        else:
            search_results = []
        ctx.search_results = search_results

        # ------------------------------------------------------------------
        # 5. Holding-relevant news enrichment (R-3) — anomaly- AND
        #    weight-driven (issue #128 narrative-layer redesign, 2026-08-20)
        # ------------------------------------------------------------------
        # After we know WHICH holdings moved, recall window news relevant to each
        # (mapping gap: a captured story that matched no macro theme), and for the
        # most-moved holdings the store has NOTHING for, run a targeted live
        # search (source gap: a window-relevant story the RSS sources never carried).
        # Both are holdings-derived, so they run AFTER Pass 1 and feed only Pass 2.
        #
        # A large holding that never crosses its own anomaly threshold used to
        # get NONE of this — anomaly_ids only. On the 2026-08-17 anchor report
        # TSM (22.5% of the portfolio, +1.22% on the day) got zero recalled
        # news and zero targeted search here, so Pass 2 wrote its TSM section
        # from prior knowledge alone. `large_weight_identifiers` is the same
        # top-K-by-weight selection L1 already uses (`ticker_intel.py`'s
        # weight channel) so a big holding gets material without needing an
        # anomaly — unioned into `anomaly_ids` so it reaches Pass 2's OWN
        # inputs too, not just L1's shared cache.
        anomaly_ids = [a["identifier"] for a in ctx.price_anomalies if a.get("identifier")]
        weight_ids = large_weight_identifiers(
            list(ctx.portfolio_summary.get("holdings") or []),
            float(ctx.portfolio_summary.get("total_base") or 0.0),
        )
        material_ids = list(dict.fromkeys([*anomaly_ids, *weight_ids]))
        recalled = recall_holding_news(news_items, material_ids)
        ctx.holding_news = {ident: _serialize_news(items) for ident, items in recalled.items()}
        logger.info(
            "report %s: recalled holding news for %d/%d moved+large holdings",
            report.id,
            len(ctx.holding_news),
            len(material_ids),
        )

        targeted = _targeted_anomaly_queries(ctx.price_anomalies, set(ctx.holding_news.keys()))
        # Large-weight holdings never reach `_targeted_anomaly_queries` above
        # (it only iterates `ctx.price_anomalies`) — give the un-recalled ones
        # the same ticker-driven query, capped independently so a portfolio
        # with both a busy anomaly day AND several large quiet holdings can't
        # let one channel starve the other.
        already_targeted = {ident for ident, _q in targeted}
        weight_targeted = [
            (ident, f"{ident} stock news catalyst")
            for ident in weight_ids
            if ident not in ctx.holding_news and ident not in already_targeted
        ][:_MAX_WEIGHT_TARGETED_SEARCHES]
        targeted = targeted + weight_targeted
        # L1 runs its OWN recall below (§5.5) over its own identifier
        # vocabulary, so all that's collected here is the targeted-search
        # titles keyed by the identifier that asked for them. `ctx.holding_news`
        # itself is never mutated — it is Pass 2's stored input, and A2's
        # report content must stay byte-identical (design doc §1.2).
        l1_targeted_titles: dict[str, list[str]] = {}
        if targeted:
            # Review round 1 bug: this used to be `daily_remaining -
            # len(ctx.search_results)` — subtracting a RESULT-ITEM count (up
            # to 5/query) from an HTTP-CALL budget, and pre-slicing the query
            # list by that wrong number before the cache-first loop even ran
            # (dropping queries that would have been free cache hits).
            # Re-derive the real remaining HTTP-call budget from actual
            # spend so far today (the Pass 1 search above may have written
            # new search_cache rows) and pass every targeted query through
            # unsliced — `_run_tavily_search`'s own cache-first loop decides
            # per query whether it needs the budget at all.
            targeted_budget = max(
                0, settings.TAVILY_DAILY_BUDGET - _tavily_used_today(session, eff_date)
            )
            query_to_identifier = {q: ident for ident, q in targeted}
            tq = [q for _ident, q in targeted]
            logger.info(
                "report %s: %d targeted anomaly searches (budget %d)",
                report.id,
                len(tq),
                targeted_budget,
            )
            targeted_results = _run_tavily_search(session, tq, eff_date, budget=targeted_budget)
            ctx.search_results.extend(targeted_results)
            for r in targeted_results:
                ident = query_to_identifier.get(r.get("query", ""))
                if ident:
                    l1_targeted_titles.setdefault(ident, []).append(r.get("title", ""))

        # Re-index results globally for [S#] citation notation
        for i, r in enumerate(ctx.search_results):
            r["index"] = i + 1

        # ------------------------------------------------------------------
        # 5.5 L1 shared ticker intel (issue #128 A2) — computed once per
        # (identifier, trade_date) across the whole system and cached
        # (ticker_intel table), so a multi-user fan-out sharing an
        # identifier pays for one LLM analysis, not one per user. Does NOT
        # feed into the Pass 2 prompt or the rendered body yet — A2 is
        # cache-infrastructure only (design doc §1.2: report content stays
        # byte-identical through A1-A3); A4 is what assembles this into the
        # report. A blocked/failed identifier degrades to "no L1 intel this
        # run", never blocks report generation.
        # ------------------------------------------------------------------
        #
        # Inputs are assembled GLOBALLY, never re-derived from the per-user
        # anomaly structures above, AND day-scoped, never window-scoped
        # (design doc §4.8, second addendum — see ticker_intel.py's module
        # docstring for the full rationale):
        #   - which identifiers: `l1_identifiers_for_user` (returns list[str],
        #     the only channel the per-user list has into the shared cache)
        #   - every number: `resolve_global_moves` called with
        #     `day_window_bounds(eff_date)`, NOT `period_start`/`period_end`.
        #     `period_start = user_watermark(user_id)` is per-user — two users
        #     analyzing the same identifier on the same `eff_date` could get
        #     different report windows, and whichever `generate_report` call
        #     reached L1 first would cache ITS window's numbers for every
        #     other user that day (round-5 review bug). `day_window_bounds`
        #     is a pure function of `eff_date` alone, so this cannot happen —
        #     and because `eff_date` is shared across a whole fan-out batch,
        #     this call also hits `moves_cache` for every user analyzing the
        #     same day, not just the anomaly-detection call above.
        #   - news: L1 recalls its OWN, via `load_day_news` over a weekday
        #     lookback ending on `eff_date` (`lookback_trading_dates`) —
        #     never `news_items` (which is per-user: `load_news_window`
        #     filters by THIS user's own `period_start`/`period_end` AND
        #     excludes whatever THIS user's `news_surfaced` ledger already
        #     marked seen). Two users would get different candidate news
        #     sets from `news_items` even on an identical price window.
        #     `load_day_news` takes no `user_id` at all, so there is
        #     nothing to diverge. Headlines are date-prefixed so the L1
        #     model can name the session they belong to.
        #     Pass 2's `ctx.holding_news` is keyed by the theme SLUG for
        #     merged entries, so re-keying it into L1's constituent
        #     vocabulary meant spraying theme headlines onto every
        #     constituent and then blocking the slug from sneaking back in
        #     as its own candidate — recalling fresh, day-scoped news per
        #     constituent sidesteps that too. Constituent-level recall works
        #     through the DESIGNED mechanism — the `holding_news_keywords.yml`
        #     alias table already maps SGOL/518660.SS/518800.SS to
        #     "gold"/"bullion" and QQQM to "Nasdaq".
        #
        # Lookback dates are a global weekday list ending on `eff_date`,
        # never this user's watermark (same contamination class as the
        # round-5 window leak). Dated own-price path + headlines go into
        # L1Facts as context; the cache key stays (identifier, trade_date,
        # l1-v4). No L2 join here (see the comment further below, at the
        # `build_l1_facts` call site, for why) — L3 is the only L1+L2 join.
        # L2 first so class-intersection extras can join the L1 candidate
        # list. L2 does not depend on L1. (issue #128 quality gate)
        l2_event_keys = l2_event_keys_for_user(session, eff_date, ctx.macro_signals)
        l2_facts = build_l2_facts(session, l2_event_keys, eff_date)
        ctx.macro_event_intel = get_l2_intel_batch(
            session,
            l2_event_keys,
            eff_date,
            l2_facts,
            usage_sink=ctx.llm_calls,
            users_remaining=users_remaining,
        )
        ctx.macro_event_exposure = user_event_exposure(
            ctx.macro_event_intel, ctx.portfolio_summary.get("by_asset_class", {})
        )
        logger.info(
            "report %s: L2 shared intel available for %d/%d candidate events, "
            "%d relevant to this portfolio",
            report.id,
            len(ctx.macro_event_intel),
            len(l2_event_keys),
            len(ctx.macro_event_exposure),
        )

        l1_identifiers = l1_identifiers_for_user(
            ctx.price_anomalies,
            holdings=list(ctx.portfolio_summary.get("holdings") or []),
            portfolio_total=float(ctx.portfolio_summary.get("total_base") or 0.0),
            exposed_asset_classes=sorted(
                {cls for classes in ctx.macro_event_exposure.values() for cls in classes}
            ),
        )
        lookback = lookback_trading_dates(eff_date, n=L1_LOOKBACK_TRADING_DAYS)
        span_news: list[NewsItem] = []
        cursor = lookback[0] if lookback else eff_date
        while cursor <= eff_date:
            span_news.extend(load_day_news(session, cursor))
            cursor += timedelta(days=1)
        l1_headlines: dict[str, list[str]] = {
            ident: [f"{n.published_at.astimezone(ET).date().isoformat()}: {n.title}" for n in items]
            for ident, items in recall_holding_news(
                span_news, l1_identifiers, max_per_holding=8
            ).items()
        }
        # Targeted-search titles attach by EXACT key only. §5's queries are keyed
        # by anomaly identifier, which is the theme slug for a merged entry — and
        # a slug is never an L1 candidate, so its results simply don't reach L1.
        # That is the point: fanning a theme's search hits out to its
        # constituents is the spraying this redesign removed, and constituents
        # get their own news through the alias table instead.
        l1_identifier_set = set(l1_identifiers)
        trade_day = eff_date.isoformat()
        for ident, titles in l1_targeted_titles.items():
            if ident in l1_identifier_set:
                l1_headlines.setdefault(ident, []).extend(
                    t if t[:10].isdigit() else f"{trade_day}: {t}" for t in titles
                )
        lookback_moves: dict[date, dict[str, HoldingMove]] = {}
        for session_date in lookback:
            day_start, day_end = day_window_bounds(session_date)
            moves, _ = resolve_global_moves(session, day_start, day_end, moves_cache)
            lookback_moves[session_date] = moves
        day_moves = lookback_moves.get(eff_date, {})

        # No L2 macro-brief join into L1 facts here (removed PR #167 review
        # round 1): `ctx.macro_event_intel`'s KEYS are this user's own L2
        # selection (`l2_event_keys_for_user` over `ctx.macro_signals`,
        # itself per-user via watermark/`news_surfaced`). Baking that
        # selection's text into a value written to the shared `ticker_intel`
        # cache meant whichever user's report reached an identifier first
        # would freeze THEIR macro-brief set into a row every later holder
        # reads — the round-5 window-leak shape in a new field. L3
        # (`get_day_synthesis`, below) already performs the L1+L2 join
        # globally, once per trading day; that is the only join point now.

        # Leftover-budget top-up (issue #128 quality gate, design doc §6.7
        # item 3). §5's targeted search covers ANOMALIES only, so an L1
        # candidate that arrived through the weight or L2-class channel — a
        # 22% holding that moved but never crossed its threshold — could not
        # reach it however hard it moved. A candidate with a large move and NO
        # recalled headline is precisely the case that is guaranteed to come
        # back [Speculative], and therefore the best use of a search nobody
        # else spent.
        #
        # Results deliberately do NOT join `ctx.search_results`: that list is
        # Pass 2's prompt input, and improving both arms of an A/B comparison
        # measures nothing (design doc §6.3.1). These titles feed L1 only.
        # Spend is still accounted for, because `_run_tavily_search` writes
        # `search_cache` rows and `_tavily_used_today` counts those.
        uncovered = sorted(
            (
                ident
                for ident in l1_identifiers
                # `ident in day_moves` is not redundant with the threshold: a
                # candidate with no captured close for `eff_date` has no move
                # at all, and `build_l1_facts` would drop it anyway — buying it
                # a search would spend the budget on a name L1 never briefs.
                if not l1_headlines.get(ident)
                and ident in day_moves
                and abs(float(day_moves[ident].net_pct)) >= _L1_SEARCH_MIN_MOVE
            ),
            key=lambda i: abs(float(day_moves[i].net_pct)),
            reverse=True,
        )[:_MAX_L1_TOPUP_SEARCHES]
        if uncovered:
            # `fair_share_budget` (PR #167 review round 3, suggestion): this
            # was the one shared-budget consumer in this function that did
            # NOT divide by `users_remaining` — L1's own analyses, L2's, and
            # L3's synthesis all do. Without it, the first
            # `active_user_ids` user in a fan-out could spend the day's
            # entire remaining Tavily budget on its own top-up searches
            # before any later user's turn — the same sequential-starvation
            # shape A4's `fair_share_budget` exists to close everywhere
            # else in this pipeline.
            topup_budget = fair_share_budget(
                max(0, settings.TAVILY_DAILY_BUDGET - _tavily_used_today(session, eff_date)),
                users_remaining,
            )
            queries = {f"{ident} stock news catalyst": ident for ident in uncovered}
            logger.info(
                "report %s: %d L1 top-up searches for un-recalled movers (budget %d)",
                report.id,
                len(queries),
                topup_budget,
            )
            for result in _run_tavily_search(session, list(queries), eff_date, budget=topup_budget):
                ident = queries.get(result.get("query", ""))
                title = result.get("title", "")
                if ident and title:
                    l1_headlines.setdefault(ident, []).append(f"{trade_day}: {title}")

        l1_facts = build_l1_facts(
            l1_identifiers,
            day_moves,
            l1_headlines,
            ctx.technical_positions,
            lookback_moves=lookback_moves,
        )
        ctx.ticker_intel = get_l1_intel_batch(
            session,
            l1_identifiers,
            eff_date,
            l1_facts,
            usage_sink=ctx.llm_calls,
            users_remaining=users_remaining,
        )
        logger.info(
            "report %s: L1 shared intel available for %d/%d candidate identifiers",
            report.id,
            len(ctx.ticker_intel),
            len(l1_identifiers),
        )

        # L2 ran immediately above L1 so class-intersection extras can join
        # the L1 candidate list (issue #128 quality gate). Selection/values
        # split is unchanged: l2_event_keys_for_user -> list[str], facts
        # from build_l2_facts (session, keys, date only).

        # ------------------------------------------------------------------
        # 5.7 L3 day-level cross-identifier synthesis (issue #128 quality
        # gate, design doc §6.7 item 1) — the ONE thing L1 (per identifier)
        # and L2 (per event) structurally cannot express: which identifiers
        # moved together today for one mechanism. Pass 2 makes that join
        # inside its single call; assembly is forbidden to invent edges, so
        # the join has to exist as a fact before assembly runs.
        # ------------------------------------------------------------------
        #
        # ORDER MATTERS: this runs AFTER `get_l1_intel_batch` because it reads
        # the day's L1 rows back out of `ticker_intel` — running it earlier
        # would analyze a day missing exactly the names this report is about.
        #
        # `get_day_synthesis(session, eff_date, ...)` takes no per-user
        # argument at all, unlike L1/L2's `*_for_user` selection channels: what
        # it analyzes ("every identifier the system briefed today") is already
        # a global fact. The per-user narrowing happens on the way OUT, via
        # `clusters_for_user`, and it happens HERE rather than downstream
        # because `ctx.cross_name_intel` is persisted to `report_inputs`, read
        # back by regenerate, and re-rendered — an unnarrowed cluster stored
        # there would outlive any later filtering.
        #
        # Wrapped: a cross-name conclusion is an enrichment, so a failure in
        # this layer costs a sentence, never a report — the same degradation
        # contract L1 and L2 answer to, except that those degrade inside their
        # own batch functions while this one is a single call.
        try:
            day_clusters = get_day_synthesis(
                session,
                eff_date,
                usage_sink=ctx.llm_calls,
                users_remaining=users_remaining,
            )
            # `all_briefed_identifiers` (PR #167 review round 1, bug 2): the
            # denylist `clusters_for_user` builds its leak guard from must be
            # every name the synthesis prompt exposed the model to, not just
            # what happened to land in a returned cluster — a summary could
            # name any of them.
            all_briefed = day_briefed_identifiers(session, eff_date)
            ctx.cross_name_intel = clusters_for_user(
                day_clusters, list(ctx.ticker_intel), all_briefed
            )
        except Exception:
            logger.exception(
                "report %s: cross-name synthesis failed — continuing without it", report.id
            )
            ctx.cross_name_intel = []
        logger.info(
            "report %s: %d cross-name cluster(s) bear on this portfolio",
            report.id,
            len(ctx.cross_name_intel),
        )

        # ------------------------------------------------------------------
        # 6. Report body — A4 personalized assembly, else Pass 2
        # ------------------------------------------------------------------
        # A4 (issue #128, design doc §6): when the shared-compute switch is on
        # and there is L1/L2 intel to work from, the body is ASSEMBLED from
        # those pre-computed analyses instead of inferred from scratch by one
        # giant per-user Pass 2 — that skipped Pass 2 call is the cost
        # reduction. `_try_assembly` returns None for every failure mode
        # (switch off, model unchosen, cold cache, provider error, truncated
        # body), so the line below degrades to the exact pre-A4 path rather
        # than to a worse report.
        raw_body = _try_assembly(client, settings, ctx, report.id)

        if raw_body is None:
            primary_model = settings.PRIMARY_LLM_MODEL
            pass2_user = _build_pass2_prompt(
                ctx.portfolio_summary,
                ctx.macro_signals,
                ctx.price_anomalies,
                ctx.search_results,
                ctx.period_start,
                ctx.period_end,
                ctx.window_trading_days,
                ctx.holding_news,
            )

            ctx.pass2_model = primary_model
            ctx.pass2_prompt = pass2_user

            logger.info("report %s: Pass 2 LLM call (%s)", report.id, primary_model)
            # Pass 2 carries holdings → enforce data_collection=deny
            raw_pass2 = _call_llm(
                client,
                primary_model,
                _PASS2_SYSTEM,
                pass2_user,
                with_holdings=True,
                usage_sink=ctx.llm_calls,
            )

            # H-DEBT-2: a provider can return a truncated HTTP 200 (rate-limiting,
            # mid-response cutoff). A short body missing §3/§4 must not ship as
            # status='success' with code-injected §2.5/§4.2/§4.4 masking the gap —
            # raise so the Celery task retries (max_retries=2). Unlike the
            # assembly path there is nothing left to fall back TO, so this stays
            # a raise.
            if body_is_incomplete(raw_pass2):
                raise RuntimeError(
                    f"report {report.id}: Pass 2 output looks truncated "
                    f"({len(raw_pass2)} chars, missing one of §3/§4)"
                )
            ctx.pass2_raw = raw_pass2
            raw_body = raw_pass2

        # Shadow comparison (design doc §6.3.1) — runs after the shipped body
        # is settled, never influences it, never blocks the report. The
        # per-model try/except inside `_run_shadow_assembly` only covers
        # `run_assembly_pass`; the isolation must also cover the ONE
        # prompt-build call before that loop (`_assembly_prompt_from_ctx`),
        # or a defect there would propagate past an already-succeeded body
        # and flip the whole report to 'failed' (round 2 review finding, PR
        # #163) — exactly the "measurement breaks what it measures" failure
        # this harness exists to rule out.
        try:
            _run_shadow_assembly(client, settings, ctx, report.id)
        except Exception:
            logger.exception(
                "report %s: shadow assembly harness failed — shipped body unaffected", report.id
            )

        # ------------------------------------------------------------------
        # 7/8. Annotate + assemble + render language + compliance scan (#5/#7/#8)
        # ------------------------------------------------------------------
        report_date_str = eff_date.strftime("%Y-%m-%d")
        full_md, violations, translated_body = _render_full_md(
            report_date_str,
            ctx.portfolio_summary,
            ctx.news_items,
            raw_body,
            output_lang,
            ctx.period_start,
            ctx.period_end,
            ctx.window_trading_days,
            ctx.price_anomalies,
            ctx.technical_positions,
            ctx.forward_events,
            ctx.price_data_through,
        )
        ctx.pass2_translated = translated_body
        logger.info("report %s: assembled + rendered (lang=%s)", report.id, output_lang)

        # ------------------------------------------------------------------
        # 9. Persist
        # ------------------------------------------------------------------
        # Compliance > everything: a body that tripped the blacklist is held as
        # 'needs_review' and never emailed — content is preserved for inspection.
        final_status = "needs_review" if violations else "success"
        report.status = final_status
        report.report_md = full_md
        report.report_inputs = ctx.to_jsonb()
        report.generated_at = datetime.now(tz=UTC)
        # H-DEBT-3 (#30): mark this window's news as surfaced in the same
        # transaction as the status commit, so the two can never diverge.
        mark_news_surfaced(session, user_id, report.id, [item.url_hash for item in news_items])
        session.commit()
        log_ops_event("report.generate.end", report_id=str(report.id), status=final_status)

        if violations:
            logger.error(
                "report %s: BLOCKED for compliance review — forbidden terms: %s",
                report.id,
                violations,
            )
            return report

        logger.info(
            "report %s: generation complete (%d chars, %d search results)",
            report.id,
            len(full_md),
            len(ctx.search_results),
        )

        # ------------------------------------------------------------------
        # 10. Email
        # ------------------------------------------------------------------
        # The report is already committed as 'success' above. send_report_email
        # is contracted never to raise, but we isolate it anyway so an unexpected
        # failure here cannot fall through to the generation-failure handler and
        # flip an already-persisted success to 'failed'.
        try:
            if not send_report_email(report, session):
                logger.warning(
                    "report %s: email sent but state unconfirmed (commit failed)",
                    report.id,
                )
        except Exception:
            logger.exception("report %s: email send raised unexpectedly", report.id)

        return report

    except Exception:
        logger.exception("report %s: generation failed", report.id)
        report.status = "failed"
        report.report_inputs = ctx.to_jsonb()
        log_ops_event("report.generate.end", report_id=str(report.id), status="failed")
        try:
            session.commit()
        except Exception:
            session.rollback()
        raise


def regenerate_report(
    session: Session,
    report_id: uuid.UUID,
    *,
    mode: str = "render",
    output_lang: str = "en",
) -> Report:
    """Rebuild an existing report from its stored inputs WITHOUT re-fetching (#6).

    Intel acquisition (news, Tavily, Pass 1) is never repeated — that data is
    read back from `report_inputs`, so no token/credit is wasted on it.

    mode='render'  : zero new LLM cost except translation. Re-runs annotation +
                     assembly + language render from the stored Pass 2 body.
                     Use it to iterate on formatting/output language.
    mode='analyze' : re-runs only the body pass from the stored inputs (no
                     fetch/Tavily/Pass 1). Use it to iterate on the body
                     prompt. Which pass runs follows the report's own
                     `body_source` (A4): Pass 2 from the stored portfolio +
                     search results, or the assembly pass from the stored
                     L1/L2 intel. Updates that pass's stored body.

    Does not email — this is an iteration/inspection tool.
    """
    user_id = get_current_user_id()
    log_ops_event("report.regenerate.start", report_id=str(report_id), mode=mode)
    report = session.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user_id)
    ).scalar_one_or_none()
    if report is None:
        raise ValueError(f"report {report_id} not found")
    inputs = cast(ReportInputsDict | None, report.report_inputs)
    # A4: `assembly_raw` is populated only when the assembly pass produced the
    # shipped body (a fallback to Pass 2 leaves it empty), so this pair reads
    # unambiguously as "the body that shipped". Pre-A4 rows carry neither key —
    # `ReportInputsDict` is total=False — and resolve to `pass2_raw` exactly as
    # before.
    stored_body = (inputs.get("assembly_raw") or inputs.get("pass2_raw") or "") if inputs else ""
    if not inputs or not stored_body:
        raise ValueError(f"report {report_id} has no stored report body to regenerate from")

    portfolio = inputs.get("portfolio_summary", {})
    news_items = inputs.get("news_items", [])

    if mode == "analyze":
        # Refresh portfolio from the live DB so holdings changes between the
        # original generation and this regenerate are picked up (ticker fixes,
        # broker corrections, new/removed rows). Pass 2 and §1 both use it.
        stored_base_ccy = inputs.get("portfolio_summary", {}).get("base_currency", "USD")
        fresh_snap = compute_portfolio(
            session,
            user_id=user_id,
            base_currency=stored_base_ccy,
            as_of=report.period_end.astimezone(ET).date() if report.period_end else None,
        )
        portfolio = _serialize_portfolio(fresh_snap)
        regen_calls: list[dict[str, Any]] = []
        period_start_iso = report.period_start.isoformat() if report.period_start else ""
        period_end_iso = report.period_end.isoformat() if report.period_end else ""
        trading_days = int(inputs.get("window_trading_days", 0))

        # A4: re-run the pass that WROTE this body, not always Pass 2. Beyond
        # being the wrong pass for an assembled report, re-running Pass 2 here
        # would write `pass2_raw` while leaving the superseded `assembly_raw`
        # in place — and since that key wins when both are present, the next
        # `mode=render` would silently rebuild the OLD report.
        if inputs.get("body_source") == "assembly":
            assembly_model = str(
                inputs.get("assembly_model") or get_settings().ASSEMBLY_LLM_MODEL
            ).strip()
            if not assembly_model:
                raise ValueError(
                    f"report {report.id}: assembled body cannot be re-analyzed — "
                    "no assembly model recorded and ASSEMBLY_LLM_MODEL is unset"
                )
            # Exposure must be recomputed against the FRESH portfolio just
            # fetched above, not replayed from the stored value (round 2
            # review finding, PR #163): the stored exposure is the
            # intersection of L2's cached classes with the ORIGINAL
            # by_asset_class, and a holdings edit between generation and
            # this regenerate can add or drop a class. Recomputing is zero
            # LLM cost (`user_event_exposure` is pure set arithmetic) —
            # only the analysis TEXT (`macro_event_intel`) stays stored,
            # since that's the part `analyze` must not re-fetch/re-derive.
            fresh_exposure = user_event_exposure(
                inputs.get("macro_event_intel", {}), portfolio.get("by_asset_class", {})
            )
            # Cross-name clusters get the same treatment for the same reason:
            # `report_inputs["cross_name_intel"]` is a PER-USER PROJECTION
            # (already narrowed to the L1 keys this user had at generation
            # time via `clusters_for_user`) — the same shape as
            # `macro_event_exposure` above, not shared mechanism text. A
            # holdings edit since generation could leave it naming a
            # position no longer held. Re-narrowing is pure set arithmetic
            # — zero LLM cost — and, exactly like `fresh_exposure` above,
            # the re-narrowed result gets WRITTEN BACK below (round-3 review
            # finding, PR #167): only re-deriving the underlying MECHANISM
            # TEXT inside each cluster (which identifiers share a mechanism,
            # and what it is) would need an LLM call and must not happen
            # here — the stored `identifiers`/`summary`/`mechanism` content
            # itself is untouched, only which clusters survive narrowing.
            #
            # `all_briefed_identifiers` here is the GENERATION-TIME
            # `ticker_intel` key set (`inputs["ticker_intel"]`), not the
            # fresh portfolio's identifiers and not a re-fetch of "everything
            # briefed that day" (this is a re-render, no Session-scoped
            # re-query of that global state is appropriate here). It is a
            # sound upper bound: the stored summary was already validated at
            # generation to name nothing outside that exact set, so re-
            # checking against it (rather than the now-possibly-smaller
            # `still_held`) still catches a name that was legitimately
            # in-scope then but is a holding this reader no longer owns now.
            still_held = set(portfolio_identifiers(portfolio))
            fresh_clusters = clusters_for_user(
                list(inputs.get("cross_name_intel", [])),
                [k for k in inputs.get("ticker_intel", {}) if k in still_held],
                list(inputs.get("ticker_intel", {})),
            )
            assembly_user = build_assembly_prompt(
                portfolio,
                inputs.get("price_anomalies", []),
                inputs.get("ticker_intel", {}),
                inputs.get("macro_event_intel", {}),
                fresh_exposure,
                period_start_iso,
                period_end_iso,
                trading_days,
                inputs.get("technical_positions", []),
                fresh_clusters,
            )
            raw_body = run_assembly_pass(
                _openrouter_client(), assembly_model, assembly_user, usage_sink=regen_calls
            )
            if body_is_incomplete(raw_body):
                raise RuntimeError(
                    f"report {report.id}: regenerated assembly output looks truncated "
                    f"({len(raw_body)} chars, missing one of §3/§4)"
                )
            body_update: dict[str, Any] = {
                "assembly_raw": raw_body,
                "assembly_prompt": assembly_user,
                "assembly_model": assembly_model,
                "assembly_prompt_version": ASSEMBLY_PROMPT_VERSION,
                # Persist the exposure actually used, not the stale value —
                # otherwise the stored row and the prompt that produced its
                # body disagree, and a later render/audit would see the
                # OLD intersection again.
                "macro_event_exposure": fresh_exposure,
                # Same reasoning, same fix (round-3 review finding, PR
                # #167): persist the re-narrowed projection actually sent
                # to the prompt, not the stale value from before whatever
                # holdings edit triggered this regenerate.
                "cross_name_intel": fresh_clusters,
            }
        else:
            pass2_user = _build_pass2_prompt(
                portfolio,
                inputs.get("macro_signals", {}),
                inputs.get("price_anomalies", []),
                inputs.get("search_results", []),
                period_start_iso,
                period_end_iso,
                trading_days,
                inputs.get("holding_news", {}),
            )
            raw_body = _call_llm(
                _openrouter_client(),
                get_settings().PRIMARY_LLM_MODEL,
                _PASS2_SYSTEM,
                pass2_user,
                with_holdings=True,
                usage_sink=regen_calls,
            )
            if body_is_incomplete(raw_body):
                raise RuntimeError(
                    f"report {report.id}: regenerated Pass 2 output looks truncated "
                    f"({len(raw_body)} chars, missing one of §3/§4)"
                )
            body_update = {"pass2_raw": raw_body, "pass2_prompt": pass2_user}

        # Recompute technical positions from the live DB so a backfill run
        # between the original generation and this regenerate is reflected.
        fresh_technical = _serialize_technical(
            compute_technical_positions(session, portfolio.get("holdings", []), report.report_date)
        )
        # New dict identity so SQLAlchemy flags the JSONB column dirty (an
        # in-place mutation of the existing dict would not be detected).
        report.report_inputs = {
            **inputs,
            **body_update,
            "llm_calls": regen_calls,
            "technical_positions": fresh_technical,
            "portfolio_summary": portfolio,
        }
        technical_positions = fresh_technical
    elif mode == "render":
        raw_body = stored_body
        technical_positions = inputs.get("technical_positions", [])
    else:
        raise ValueError(f"unknown mode {mode!r} (expected 'render' or 'analyze')")

    report_date_str = report.report_date.strftime("%Y-%m-%d")
    full_md, violations, translated_body = _render_full_md(
        report_date_str,
        portfolio,
        news_items,
        raw_body,
        output_lang,
        report.period_start.isoformat() if report.period_start else "",
        report.period_end.isoformat() if report.period_end else "",
        int(inputs.get("window_trading_days", 0)),
        inputs.get("price_anomalies", []),
        technical_positions,
        inputs.get("forward_events", []),
        str(inputs.get("price_data_through", "")),
    )
    report.status = "needs_review" if violations else "success"
    report.report_md = full_md
    # Persist translation snapshot alongside report_md for compliance traceability.
    if report.report_inputs is not None:
        report.report_inputs = {**report.report_inputs, "pass2_translated": translated_body}
    report.generated_at = datetime.now(tz=UTC)
    session.commit()
    logger.info("report %s: regenerated (mode=%s, lang=%s)", report.id, mode, output_lang)
    return report
