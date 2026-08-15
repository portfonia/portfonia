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
from app.services.email_sender import send_ops_alert, send_report_email
from app.services.forward_events import load_forward_events
from app.services.github_issues import create_bug_report
from app.services.holding_news import recall_holding_news
from app.services.macro_detector import detect_macro_signals
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import compute_portfolio
from app.services.price_anomaly_detector import PriceAnomaly
from app.services.report_context import ReportContext, ReportInputsDict
from app.services.report_llm import _BYOK_PROVIDER_ORDER, _call_llm, _openrouter_client
from app.services.report_prompts import (
    _COMPLIANCE_SYSTEM_PREFIX,
    _PASS2_MIN_CHARS,
    _PASS2_REQUIRED_MARKERS,
    _PASS2_SYSTEM,
    _build_pass1_prompt,
    _build_pass2_prompt,
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
from app.services.technical_position import compute_technical_positions
from app.services.window_data import (
    MovesCache,
    detect_window_anomalies,
    latest_window_close_date,
    load_news_window,
    mark_news_surfaced,
    unmark_news_surfaced,
    user_watermark,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPT_VERSION = "f2-v6"  # f2-v6: §4.2 cross-reference restricted to holdings actually in the anomaly table (R-8) + HOLDING-RELEVANT NEWS block from per-holding recall/targeted search (R-3); f2-v5 = direction-requires-evidence + divergence-is-the-signal (no price-direction claims without window data); f2-v4 = §4.2 code table + driver-only, evidence confidence labels, §4.4 technical position
_DISCLAIMER_VERSION = "f3-bilingual-v2"

# Forward calendar (#1): how far ahead §2.5 looks. The capture task fetches a
# wider horizon so the read is always populated within this window.
_FORWARD_WINDOW_DAYS = 10


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
    """
    validate_report_type(report_type)
    settings = get_settings()
    user_id = user_id if user_id is not None else get_current_user_id()
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
            session, eff_date, eff_date + timedelta(days=_FORWARD_WINDOW_DAYS)
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
        used_today = _tavily_used_today(session, eff_date, exclude_report_id=report.id)
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
            search_results = _run_tavily_search(ctx.search_queries, budget=daily_remaining)
        else:
            search_results = []
        ctx.search_results = search_results

        # ------------------------------------------------------------------
        # 5. Holding-relevant news enrichment (R-3) — anomaly-driven
        # ------------------------------------------------------------------
        # After we know WHICH holdings moved, recall window news relevant to each
        # (mapping gap: a captured story that matched no macro theme), and for the
        # most-moved holdings the store has NOTHING for, run a targeted live
        # search (source gap: a window-relevant story the RSS sources never carried).
        # Both are holdings-derived, so they run AFTER Pass 1 and feed only Pass 2.
        anomaly_ids = [a["identifier"] for a in ctx.price_anomalies if a.get("identifier")]
        recalled = recall_holding_news(news_items, anomaly_ids)
        ctx.holding_news = {ident: _serialize_news(items) for ident, items in recalled.items()}
        logger.info(
            "report %s: recalled holding news for %d/%d moved holdings",
            report.id,
            len(ctx.holding_news),
            len(anomaly_ids),
        )

        targeted = _targeted_anomaly_queries(ctx.price_anomalies, set(ctx.holding_news.keys()))
        targeted_remaining = max(0, daily_remaining - len(ctx.search_results))
        if targeted and targeted_remaining > 0:
            tq = [q for _ident, q in targeted][:targeted_remaining]
            logger.info("report %s: %d targeted anomaly searches", report.id, len(tq))
            targeted_results = _run_tavily_search(tq, budget=targeted_remaining)
            ctx.search_results.extend(targeted_results)

        # Re-index results globally for [S#] citation notation
        for i, r in enumerate(ctx.search_results):
            r["index"] = i + 1

        # ------------------------------------------------------------------
        # 6. Pass 2 — full report body
        # ------------------------------------------------------------------
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
        # raise so the Celery task retries (max_retries=2).
        if len(raw_pass2) < _PASS2_MIN_CHARS or not all(
            marker in raw_pass2 for marker in _PASS2_REQUIRED_MARKERS
        ):
            raise RuntimeError(
                f"report {report.id}: Pass 2 output looks truncated "
                f"({len(raw_pass2)} chars, missing one of {_PASS2_REQUIRED_MARKERS})"
            )
        ctx.pass2_raw = raw_pass2

        # ------------------------------------------------------------------
        # 7/8. Annotate + assemble + render language + compliance scan (#5/#7/#8)
        # ------------------------------------------------------------------
        report_date_str = eff_date.strftime("%Y-%m-%d")
        full_md, violations, translated_body = _render_full_md(
            report_date_str,
            ctx.portfolio_summary,
            ctx.news_items,
            raw_pass2,
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
    mode='analyze' : re-runs only Pass 2 from the stored portfolio + search
                     results (no fetch/Tavily/Pass 1). Use it to iterate on the
                     Pass 2 prompt. Updates the stored Pass 2 body.

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
    if not inputs or not inputs.get("pass2_raw"):
        raise ValueError(f"report {report_id} has no stored Pass 2 body to regenerate from")

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

        pass2_user = _build_pass2_prompt(
            portfolio,
            inputs.get("macro_signals", {}),
            inputs.get("price_anomalies", []),
            inputs.get("search_results", []),
            report.period_start.isoformat() if report.period_start else "",
            report.period_end.isoformat() if report.period_end else "",
            int(inputs.get("window_trading_days", 0)),
            inputs.get("holding_news", {}),
        )
        regen_calls: list[dict[str, Any]] = []
        raw_body = _call_llm(
            _openrouter_client(),
            get_settings().PRIMARY_LLM_MODEL,
            _PASS2_SYSTEM,
            pass2_user,
            with_holdings=True,
            usage_sink=regen_calls,
        )
        if len(raw_body) < _PASS2_MIN_CHARS or not all(
            marker in raw_body for marker in _PASS2_REQUIRED_MARKERS
        ):
            raise RuntimeError(
                f"report {report.id}: regenerated Pass 2 output looks truncated "
                f"({len(raw_body)} chars, missing one of {_PASS2_REQUIRED_MARKERS})"
            )
        # Recompute technical positions from the live DB so a backfill run
        # between the original generation and this regenerate is reflected.
        fresh_technical = _serialize_technical(
            compute_technical_positions(session, portfolio.get("holdings", []), report.report_date)
        )
        # New dict identity so SQLAlchemy flags the JSONB column dirty (an
        # in-place mutation of the existing dict would not be detected).
        report.report_inputs = {
            **inputs,
            "pass2_raw": raw_body,
            "pass2_prompt": pass2_user,
            "llm_calls": regen_calls,
            "technical_positions": fresh_technical,
            "portfolio_summary": portfolio,
        }
        technical_positions = fresh_technical
    elif mode == "render":
        raw_body = inputs["pass2_raw"]
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
