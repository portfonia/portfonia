"""LLM report generation pipeline (Ring 0 — Stage F2).

Report design:
  Collect  scheduled intel slots capture news, accepted article bodies and L1/L2/L3
  Read     report-time code reads only the current user's URL-free material/cache rows
  Pass 2  portfolio snapshot + scheduled material + anomalies → PRIMARY_LLM → §2/§3/§4 body
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
  - Holdings-derived material is scoped to the requesting user's Pass 2 context.
  - OPENROUTER_DATA_COLLECTION = "deny" is enforced on every LLM call.

Orchestration only (#37): prompt text, code-built section renderers, the LLM
transport, serialization, and the compliance/translation
backstops each live in their own module — see report_prompts.py,
report_sections.py, report_llm.py, report_search.py, report_serializers.py,
app/compliance/output_scan.py, and report_translation.py. This file wires
them together into generate_report()/regenerate_report().
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import extract, select
from sqlalchemy.orm import Session

from app.compliance.output_scan import (
    _scan_forbidden_output,
    _strip_body_disclaimer,
    _strip_markers,
)
from app.core import operational_events as oe
from app.core.alert_dedup import already_alerted, mark_alerted
from app.core.config import get_settings
from app.core.ops_log import log_ops_event
from app.core.timezones import ET
from app.models.intel import IntelSlotRun
from app.models.paid_intel import IntelArticle, IntelArticleLink
from app.models.report import Report
from app.services.analysis_framework import load_analysis_framework
from app.services.cross_name_intel import (
    clusters_for_user,
    day_briefed_identifiers,
    read_day_synthesis,
)
from app.services.email_sender import send_ops_alert, send_report_email
from app.services.forward_events import FORWARD_WINDOW_DAYS, load_forward_events
from app.services.github_issues import create_bug_report
from app.services.holding_news import load_entity_aliases, recall_holding_news
from app.services.investment_context import InvestorPreferences, load_investor_preferences
from app.services.macro_coverage import (
    extract_macro_sidecar,
    load_recent_macro_coverage,
    persist_macro_coverage,
)
from app.services.macro_detector import detect_macro_signals
from app.services.macro_event_intel import (
    l2_event_keys_for_user,
    read_l2_intel,
    user_event_exposure,
)
from app.services.news_fetcher import NewsItem
from app.services.portfolio_calculator import compute_portfolio
from app.services.price_anomaly_detector import PriceAnomaly
from app.services.report_assembly import (
    ASSEMBLY_PROMPT_VERSION,
    _identifier,
    _weight,
    build_assembly_prompt,
    parse_shadow_models,
    portfolio_identifiers,
    run_assembly_pass,
    should_use_assembly,
)
from app.services.report_context import ReportContext, ReportInputsDict
from app.services.report_llm import (  # noqa: F401 - retained for legacy test fixture patching
    _call_llm,
    _call_llm_byok_with_fallback,
    _openrouter_client,
)
from app.services.report_prompts import (
    _build_pass2_prompt,
    _build_pass2_system,
    body_is_incomplete,
    prompt_label_hits,
)
from app.services.report_search import _run_tavily_search  # noqa: F401 - legacy test patch target
from app.services.report_sections import (
    _build_data_window,
    _build_footer,
    _build_forward_block,
    _build_section1,
    _build_section1_page_links,
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
from app.services.section3_proportionality import (
    HoldingCheckInput,
    check_section3_proportionality,
)
from app.services.technical_position import compute_technical_positions
from app.services.ticker_intel import (
    l1_identifiers_for_user,
    large_weight_identifiers,
    read_l1_intel,
)
from app.services.watch_tier_config import load_watch_tier_weights
from app.services.window_data import (
    HoldingMove,
    MovesCache,
    backfill_news_surfaced_before,
    cold_start_watermark,
    detect_window_anomalies,
    latest_window_close_date,
    load_instrument_news_by_identifier,
    load_news_window,
    mark_news_surfaced,
    resolve_global_moves,
    unmark_news_surfaced,
    user_watermark,
)
from app.services.zh_hant import to_traditional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROMPT_VERSION = (
    "f2-v12"  # Issue #639: grounded direction, section-label suppression and rolling moves.
)


def _serialize_holding_move(move: HoldingMove) -> dict[str, Any]:
    return {
        "net_pct": float(move.net_pct),
        "max_day_pct": float(move.max_day_pct) if move.max_day_pct is not None else None,
        "max_day_date": move.max_day_date.isoformat() if move.max_day_date is not None else None,
    }


MAX_HOLDINGS_WITH_HEADLINES = 6

_DISCLAIMER_VERSION = "f3-bilingual-v2"


def intel_trade_date(session: Session, eff_date: date) -> date | None:
    """Return the latest completed weekday post-close slot available to a report.

    Slots run every day, but only weekday post_close runs compute L1/L2/L3
    (`intel_tasks`: `not weekend and slot == "post_close"`), so a weekend run
    is skipped here; a Saturday weekly report reads Friday's caches.
    """
    return session.scalar(
        select(IntelSlotRun.run_date)
        .where(
            IntelSlotRun.slot == "post_close",
            IntelSlotRun.status.in_(["ok", "partial"]),
            IntelSlotRun.run_date <= eff_date,
            extract("isodow", IntelSlotRun.run_date) <= 5,
        )
        .order_by(IntelSlotRun.run_date.desc())
        .limit(1)
    )


def _holding_order(ctx: ReportContext) -> list[str]:
    holdings = list(ctx.portfolio_summary.get("holdings") or [])
    total = float(ctx.portfolio_summary.get("total_base") or 0.0)
    anomalies = sorted(
        (a for a in ctx.price_anomalies if a.get("identifier")),
        key=lambda a: -abs(float(a.get("window_net_pct") or a.get("pct_change") or 0.0)),
    )
    identifiers = [str(a["identifier"]) for a in anomalies]
    large = large_weight_identifiers(holdings, total)
    identifiers.extend(large)
    weighted: dict[str, float] = {}
    for holding in holdings:
        identifier = str(holding.get("ticker") or holding.get("fund_code") or "")
        if identifier:
            weighted[identifier] = weighted.get(identifier, 0.0) + float(
                holding.get("market_value_base") or 0.0
            )
    identifiers.extend(
        identifier for identifier, _ in sorted(weighted.items(), key=lambda x: -x[1])
    )
    return list(dict.fromkeys(identifiers))


def _holding_headline_order(ctx: ReportContext) -> list[str]:
    stocks = [
        h for h in ctx.portfolio_summary.get("holdings", []) if h.get("asset_type") == "stock"
    ]
    weights: dict[str, float] = {}
    for holding in stocks:
        identifier = _identifier(holding)
        if identifier:
            weights[identifier] = weights.get(identifier, 0.0) + float(
                holding.get("market_value_base") or 0.0
            )
    moves: dict[str, float] = {}
    for anomaly in ctx.price_anomalies:
        members = anomaly.get("constituents") or [anomaly]
        for member in members:
            identifier = str(member.get("identifier") or "")
            if identifier in weights:
                moves[identifier] = abs(
                    float(member.get("window_net_pct") or member.get("pct_change") or 0.0)
                )
    return sorted(
        weights,
        key=lambda identifier: (
            (0, -moves[identifier]) if identifier in moves else (1, -weights[identifier])
        ),
    )


def prompt_label_alert_body(reports: Sequence[Report]) -> str:
    lines = []
    for report in reports:
        inputs = report.report_inputs
        hits = inputs.get("prompt_label_hits") if isinstance(inputs, dict) else None
        if isinstance(hits, list) and hits:
            lines.append(
                f"Report {report.id}, user {report.user_id}: " + ", ".join(str(hit) for hit in hits)
            )
    return "\n".join(lines)


def _article_entry(article: IntelArticle, query: str, index: int) -> dict[str, Any] | None:
    record = article.record or {}
    title = record.get("title")
    body = record.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    published_at = record.get("published_at")
    return {
        "query": query,
        "title": title,
        "published_at": published_at if isinstance(published_at, str) else None,
        "content": body,
        "score": 0.0,
        "index": index,
        "article_id": str(article.id),
    }


def _load_report_articles(
    session: Session,
    ctx: ReportContext,
    period_start: datetime,
    period_end: datetime,
) -> list[dict[str, Any]]:
    runs = select(IntelSlotRun.id).where(
        IntelSlotRun.started_at > period_start,
        IntelSlotRun.started_at <= period_end,
    )
    seen_articles: set[uuid.UUID] = set()
    seen_url_keys: set[str] = set()
    entries: list[dict[str, Any]] = []
    for identifier in _holding_order(ctx):
        rows = session.execute(
            select(IntelArticle)
            .join(IntelArticleLink, IntelArticleLink.article_id == IntelArticle.id)
            .where(
                IntelArticle.slot_run_id.in_(runs),
                IntelArticle.status == "accepted",
                IntelArticleLink.identifier == identifier,
            )
            .order_by(IntelArticle.fetched_at.desc())
        ).scalars()
        local_keys: set[str] = set()
        for article in rows:
            if (
                article.id in seen_articles
                or article.url_key in local_keys
                or article.url_key in seen_url_keys
            ):
                continue
            entry = _article_entry(article, identifier, len(entries) + 1)
            if entry is None:
                continue
            entries.append(entry)
            seen_articles.add(article.id)
            local_keys.add(article.url_key)
            seen_url_keys.add(article.url_key)
            if len(local_keys) >= 2 or len(entries) >= 15:
                break
        if len(entries) >= 15:
            break

    themes = [
        str(hit["theme"])
        for hit in ctx.macro_signals.get("hits", [])
        if isinstance(hit, dict) and hit.get("theme")
    ]
    macro_count = 0
    for theme in themes:
        rows = session.execute(
            select(IntelArticle)
            .join(IntelArticleLink, IntelArticleLink.article_id == IntelArticle.id)
            .where(
                IntelArticle.slot_run_id.in_(runs),
                IntelArticle.status == "accepted",
                IntelArticleLink.theme == theme,
            )
            .order_by(IntelArticle.fetched_at.desc())
        ).scalars()
        local = 0
        for article in rows:
            if article.id in seen_articles or article.url_key in seen_url_keys:
                continue
            entry = _article_entry(article, f"theme:{theme}", len(entries) + 1)
            if entry is None:
                continue
            entries.append(entry)
            seen_articles.add(article.id)
            seen_url_keys.add(article.url_key)
            local += 1
            macro_count += 1
            if local >= 2 or macro_count >= 6:
                break
        if macro_count >= 6:
            break
    return entries


def _normalized_news_title(title: str) -> str:
    return re.sub(r"[\W_]+", " ", title.casefold()).strip()


def _merge_holding_news(
    keyword_news: list[NewsItem], linked_news: dict[str, list[NewsItem]], identifiers: list[str]
) -> tuple[dict[str, list[NewsItem]], set[str]]:
    all_items = [*keyword_news, *(item for items in linked_news.values() for item in items)]
    by_identifier: dict[str, list[NewsItem]] = {}
    all_hashes = {item.url_hash for item in all_items}
    for identifier in identifiers:
        matched = [
            *recall_holding_news(keyword_news, [identifier], max_per_holding=len(keyword_news)).get(
                identifier, []
            ),
            *linked_news.get(identifier, []),
        ]
        seen_hashes: set[str] = set()
        seen_titles: set[str] = set()
        selected: list[NewsItem] = []
        for item in sorted(matched, key=lambda item: item.published_at, reverse=True):
            normalized = _normalized_news_title(item.title)
            if item.url_hash in seen_hashes or (normalized and normalized in seen_titles):
                continue
            seen_hashes.add(item.url_hash)
            if normalized:
                seen_titles.add(normalized)
            selected.append(item)
            if len(selected) == 6:
                break
        if selected:
            by_identifier[identifier] = selected
    return by_identifier, all_hashes


# Forward calendar (#1): how far ahead §2.5 looks — now defined once in
# forward_events.py, since the L2 shared cache (issue #128 A3) must analyze
# exactly the horizon this section renders (round-1 review nit, PR #157: two
# copies of the same 10 could drift into two different horizons).


# ---------------------------------------------------------------------------
# Assembly / rendering (shared by live generation and re-render)
# ---------------------------------------------------------------------------


# PR #423 review (blacktomb42): §3 prose almost never repeats a holding's
# full legal name ("Apple Inc.") — it says "Apple". A literal-phrase match
# on `holding.name` alone would still leave most real holdings unmatched.
# Stripping a common trailing legal-entity suffix (repeatedly, so
# "X Holdings Group" also reduces) gives a second, shorter alias term
# alongside the full name — not a replacement for it, since some display
# names ARE already the short form and stripping nothing is correct there.
_LEGAL_NAME_SUFFIXES = (
    " Incorporated",
    " Corporation",
    " Inc.",
    " Inc",
    " Corp.",
    " Corp",
    " Co., Ltd.",
    " Co Ltd",
    " Ltd.",
    " Ltd",
    " PLC",
    " plc",
    " Holdings",
    " Holding",
    " Group",
    "股份有限公司",
    "有限公司",
    "控股",
    "集团",
)


def _core_name(display_name: str) -> str:
    """`display_name` with one or more trailing legal-entity suffixes
    stripped — "Apple Inc." -> "Apple", "腾讯控股" -> "腾讯". Returns
    `display_name` unchanged when no known suffix matches."""
    core = display_name
    stripped = True
    while stripped:
        stripped = False
        for suffix in _LEGAL_NAME_SUFFIXES:
            if core.endswith(suffix):
                core = core[: -len(suffix)].rstrip(" ,.")
                stripped = True
    return core


# Dedup key has no state-varying component beyond the date (issue #421 PR
# #425 review soft note 2) — there is exactly one config file, so "broken"
# is a single day-scoped condition, not a per-fingerprint one like fx_
# fetcher.py's per-pair alerts. TTL is a GC safety net only, same convention
# as fx_fetcher._ALERT_DEDUP_TTL_SECONDS/price_capture.py (issue #298).
_WATCH_TIER_CONFIG_ALERT_DEDUP_TTL_SECONDS = 90 * 24 * 60 * 60


def _load_watch_tier_weights_or_alert() -> dict[str, float]:
    """`load_watch_tier_weights()`, but a broken/unreadable config must not
    disappear into `_render_full_md`'s outer log-only try/except (PR #425
    review soft note 2) — ops needs to know a watched holding's §3 floor
    was skipped this run, not just find it in worker.log after the fact.

    Fails soft for the report itself: every watched holding falls back to
    its real weight for this run (same as `_build_holding_check_inputs`
    treats watch_tier=None), matching the product decision that report
    generation must still complete. Mirrors fx_fetcher.py's `_send_fx_alert`
    exactly: production-gated, durable Redis dedup (`already_alerted`/
    `mark_alerted`, issue #298) keyed per calendar day (ET) so a persisting
    break alerts once per day, not once per report."""
    try:
        return load_watch_tier_weights()
    except Exception as exc:
        logger.exception("watch_tier_weights config failed to load — §3 floor skipped this run")
        if get_settings().APP_ENV != "production":
            return {}
        today_et = datetime.now(tz=ET).date().isoformat()
        dedup_key = f"ops-watch-tier-weights-config-broken-{today_et}"
        if already_alerted(dedup_key):
            return {}
        if send_ops_alert(
            subject="[Portfonia] watch_tier_weights config broken",
            body=(
                f"watch_tier_weights.yml failed to load: {type(exc).__name__}: {exc}\n\n"
                "Every watch_tier-tagged holding's §3 proportionality check fell "
                "back to its real position weight this run — no config floor "
                "applied — until this is fixed.\n\n"
                "Config path: backend/config/watch_tier_weights.yml "
                "(override: Settings.WATCH_TIER_WEIGHTS_CONFIG_PATH)."
            ),
            idempotency_key=dedup_key,
            severity="ALERT",
        ):
            mark_alerted(dedup_key, _WATCH_TIER_CONFIG_ALERT_DEDUP_TTL_SECONDS)
        return {}


def _build_holding_check_inputs(
    portfolio: dict[str, Any],
    holding_news: dict[str, list[dict[str, Any]]],
    anomalies: list[dict[str, Any]],
) -> list[HoldingCheckInput]:
    """Assemble issue #173's per-holding check inputs from this report's
    already-gathered data — no new fetch, no LLM call.

    `weight` is computed HERE, once, from the real portfolio (`_weight`,
    `report_assembly.py`) and handed to `HoldingCheckInput` as an explicit
    value; `check_section3_proportionality` itself never reads a holding's
    real position (issue #173 Design item 4). Issue #421 Design item 7,
    amended per PR #425 review (blacktomb42) blocker 1: a holding with
    `watch_tier` set gets `max(real_weight, watch_tier_weights.yml's
    configured weight)` HERE — a FLOOR, never an absolute substitute. The
    original "always replace" version silently SHRANK §3 depth for a
    real-sized holding tagged watched (e.g. a 40% position marked
    "critical" checked as if it were 15%), the opposite of the product
    intent (lift a near-zero tracked name, never demote a real one). This
    is still the only call site — never inside the checker, and never
    touching portfolio_calculator.py's real position math.

    `material_text` is built from `ctx.holding_news` (the code-level recall
    already scoped to this holding, issue #30/R-3) plus this holding's own
    anomaly record's trigger/theme text — both already Pass 2/assembly-stage,
    holdings-derived data, used only by the report body prompt.
    """
    total = float(portfolio.get("total_base", 0) or 0)
    entity_aliases = load_entity_aliases()
    watch_tier_weights = _load_watch_tier_weights_or_alert()
    anomalies_by_identifier: dict[str, list[dict[str, Any]]] = {}
    for anomaly in anomalies:
        ident = anomaly.get("identifier")
        if ident:
            anomalies_by_identifier.setdefault(ident, []).append(anomaly)

    inputs: list[HoldingCheckInput] = []
    for holding in portfolio.get("holdings", []):
        ident = _identifier(holding)
        if not ident:
            continue
        material_parts: list[str] = []
        for item in holding_news.get(ident, []):
            material_parts.append(str(item.get("title") or ""))
            material_parts.append(str(item.get("summary") or ""))
        for anomaly in anomalies_by_identifier.get(ident, []):
            material_parts.append(str(anomaly.get("trigger") or ""))
            material_parts.append(str(anomaly.get("theme_label_en") or ""))
        display_name = str(holding.get("name") or "").strip()
        alias_terms = [ident, *entity_aliases.get(ident, [])]
        if display_name:
            # PR #423 review (blacktomb42): §3 is flowing prose and
            # routinely names a company without repeating its ticker;
            # AAPL, for example, has no `entity_aliases` row in
            # holding_news_keywords.yml. Without the holding's own display
            # name as a matchable term, that prose silently attributes
            # zero length to a holding it actually did discuss. Both the
            # full name and its suffix-stripped core are added — real §3
            # prose almost always uses the short form ("Apple", not
            # "Apple Inc.").
            alias_terms.append(display_name)
            core = _core_name(display_name)
            if core and core != display_name:
                alias_terms.append(core)
        watch_tier = holding.get("watch_tier")
        real_weight = _weight(holding, total)
        tier_weight = watch_tier_weights.get(watch_tier) if watch_tier else None
        weight = max(real_weight, tier_weight) if tier_weight is not None else real_weight
        inputs.append(
            HoldingCheckInput(
                identifier=ident,
                alias_terms=alias_terms,
                weight=weight,
                material_text="\n".join(part for part in material_parts if part),
            )
        )
    return inputs


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
    report_id: uuid.UUID | None = None,
    holding_news: dict[str, list[dict[str, Any]]] | None = None,
    label_hits: list[str] | None = None,
) -> tuple[str, list[str], str]:
    """Annotate, assemble, language-render, and compliance-scan a report.

    Returns (full_markdown, violations, translated_body). The third element is
    the translated dynamic section (pre-footer) for compliance audit traceability.
    Pure function of its inputs — this is what makes #6 re-render possible.

    `report_id`/`holding_news` (issue #173): when both are supplied, this
    also runs the log-only §3 proportionality check against the model's
    raw §3 output (before any code-built injection below, and independent
    of the compliance scan further down — see Contract constraints). Both
    default to None so the quiet-day canned body (no real per-holding
    analysis to check) and any other caller can opt out simply by not
    passing them.
    """
    render_lang = "zh" if output_lang == "zh-Hant" else output_lang
    cleaned = _strip_markers(raw_body)
    if report_id is not None and holding_news is not None:
        # Log-only, must never break report generation — issue #173 Design
        # item 3 / Contract constraints invariant.
        try:
            check_inputs = _build_holding_check_inputs(portfolio, holding_news, anomalies or [])
            check_section3_proportionality(str(report_id), cleaned, check_inputs)
        except Exception:
            logger.exception(
                "report %s: §3 proportionality check raised — continuing (log-only, non-fatal)",
                report_id,
            )
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
    header = f"# Portfonia Holdings Briefing — {_header_timestamp(report_date_str, period_end)}\n\n"
    window = _build_data_window(
        news_items, portfolio, period_start, period_end, trading_days, price_data_through
    )
    section1 = _build_section1(portfolio)

    # Compliance scan on the English canonical first (highest-signal blacklist).
    violations = _scan_forbidden_output(cleaned)

    # Translate the snapshot and the body separately so §1's closing page-links
    # line (issue #560) is spliced in already localized and never reaches the
    # LLM. Chunking is by heading, so the chunk set is unchanged.
    snapshot_out = _translate_md(header + window + section1, render_lang)
    body_out = _translate_md(cleaned, render_lang)
    hits = prompt_label_hits(raw_body, body_out)
    if label_hits is not None:
        label_hits[:] = hits
    if hits:
        logger.warning(
            "report %s: internal prompt section names in body: %s", report_id, ", ".join(hits)
        )
    dynamic_out = (
        snapshot_out.rstrip() + "\n\n" + _build_section1_page_links(render_lang) + "\n\n" + body_out
    )
    # The translator can re-add its own disclaimer paragraph (it runs after the
    # pre-translation strip); remove it so the body carries no disclaimer and the
    # scan does not false-trip on its advisory-sounding wording in either language.
    dynamic_out = _strip_body_disclaimer(dynamic_out)
    if render_lang != "en":
        # Translation can paraphrase into advisory tone — re-scan the output.
        violations = violations + _scan_forbidden_output(dynamic_out)

    full_md = dynamic_out + _build_footer(portfolio, render_lang)
    # Both scans must see the canonical/Simplified text before Taiwan conversion.
    if output_lang == "zh-Hant":
        full_md = to_traditional(full_md)
        dynamic_out = to_traditional(dynamic_out)
    return full_md, violations, dynamic_out


# ---------------------------------------------------------------------------
# A4 personalized assembly (issue #128, design doc §6)
# ---------------------------------------------------------------------------


def _assembly_prompt_from_ctx(ctx: ReportContext, investor_prefs: InvestorPreferences) -> str:
    """Build the assembly prompt from THIS report's context and nothing else.

    Every argument is a `ctx` field already scoped to one user — the shared
    caches are read through `ctx.ticker_intel`/`ctx.macro_event_intel`, which
    `l1_identifiers_for_user`/`l2_event_keys_for_user` narrowed to this user's
    own candidates. `build_assembly_prompt` takes no Session precisely so this
    stays the only way in (see report_assembly.py's docstring).

    `investor_prefs` is passed separately, not read off `ctx` (PR #212
    review finding, issue #129 checkpoint B6): `investor_prefs.free_text`
    must never land on `ctx`, since `ctx.to_jsonb()` serializes every
    dataclass field into `report_inputs` — an unencrypted column — and
    free_text is the one encrypted field on user_investment_context.
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
        investor_locale=investor_prefs.locale,
        investor_questionnaire=investor_prefs.questionnaire,
        investor_free_text=investor_prefs.free_text,
        macro_signals=ctx.macro_signals,
        macro_continuity=ctx.macro_continuity_snapshot,
    )


def _try_assembly(
    client: Any,
    settings: Any,
    ctx: ReportContext,
    report_id: uuid.UUID,
    investor_prefs: InvestorPreferences,
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
    prompt = _assembly_prompt_from_ctx(ctx, investor_prefs)
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
    client: Any,
    settings: Any,
    ctx: ReportContext,
    report_id: uuid.UUID,
    investor_prefs: InvestorPreferences,
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

    prompt = _assembly_prompt_from_ctx(ctx, investor_prefs)
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


def _finish_report(
    session: Session,
    report: Report,
    ctx: ReportContext,
    user_id: uuid.UUID,
    eff_date: date,
    output_lang: str,
    raw_body: str,
    news_items: list[NewsItem] | None,
    *,
    stage_state: dict[str, str] | None = None,
    extra_url_hashes: set[str] | None = None,
) -> Report:
    """Render, persist, mark news surfaced, and email — generate_report's
    common tail (steps 7/8/9/10), shared by the full pipeline and the
    stage-skip-on-retry path (#61).

    `stage_state` (issue #446): the caller's compact stage-completion map,
    mutated in place as render_and_compliance/persist_report/email_send are
    entered — both `generate_report` call sites pass their own dict.
    `regenerate_report` never calls this function at all (see its own
    docstring above), so no operational-event participation is needed for
    a `None` case here in practice; kept optional only so a future direct
    test of this function does not need to fabricate one.

    `news_items` is the NewsItem list `mark_news_surfaced` needs (it reads
    `.url_hash`). The full pipeline has it from `load_news_window`; the
    stage-skip path skips that call entirely (nothing upstream of the stored
    body is re-fetched), so it passes None here and reads stored `url_hash`
    values from both the report news pool and the recalled holding news.

    NOT reused by `regenerate_report()` (PR #341 review): that function
    deliberately never emails and never calls `mark_news_surfaced` — it is
    an on-demand iteration/inspection tool, not a generation attempt, and
    merges into the row's EXISTING `report_inputs` rather than writing a
    fresh `ctx.to_jsonb()`. Folding it into this shared tail would either
    have to thread a "skip email/mark" flag through every caller or silently
    change its no-side-effects contract — its own small persist block at the
    end stays separate on purpose.

    `raw_body` (issue #440): still carries the macro-coverage sidecar the
    §2-writing pass appended (`ctx.pass2_raw`/`ctx.assembly_raw` store it
    verbatim, sidecar included, for audit). Extracted HERE — the single
    render point for both the live pipeline and the #61 resume path — so a
    malformed or missing sidecar can never leak into the rendered report,
    and so a resume re-derives coverage from the SAME stored raw body rather
    than needing it re-persisted at generation time.
    """
    _render_span = oe.start_span("render_and_compliance")
    visible_body, coverage_items = extract_macro_sidecar(raw_body)
    report_date_str = eff_date.strftime("%Y-%m-%d")
    full_md, violations, translated_body = _render_full_md(
        report_date_str,
        ctx.portfolio_summary,
        ctx.news_items,
        visible_body,
        output_lang,
        ctx.period_start,
        ctx.period_end,
        ctx.window_trading_days,
        ctx.price_anomalies,
        ctx.technical_positions,
        ctx.forward_events,
        ctx.price_data_through,
        report_id=report.id,
        holding_news=ctx.holding_news,
        label_hits=ctx.prompt_label_hits,
    )
    ctx.pass2_translated = translated_body
    logger.info("report %s: assembled + rendered (lang=%s)", report.id, output_lang)
    if stage_state is not None:
        stage_state["render_and_compliance"] = "degraded" if violations else "ok"
    oe.end_span(
        _render_span,
        "degraded" if violations else "ok",
        reason_code="compliance_violation" if violations else None,
    )

    # ------------------------------------------------------------------
    # 9. Persist
    # ------------------------------------------------------------------
    # Compliance > everything: a body that tripped the blacklist is held as
    # 'needs_review' and never emailed — content is preserved for inspection.
    _persist_span = oe.start_span("persist_report")
    final_status = "needs_review" if violations else "success"
    report.status = final_status
    report.report_md = full_md
    report.report_inputs = ctx.to_jsonb()
    report.generated_at = datetime.now(tz=UTC)
    # issue #440: atomic with the status/body commit below (Contract
    # constraints "Generate/retry transaction failure: No finalized
    # report/coverage half-commit"). `as_of` is the report's own evidence
    # cutoff (period_end), never generated_at — Design §4. Persisted
    # regardless of `final_status` (write-time is uniform; eligibility for a
    # LATER report's continuity read is the gate — success-only, see
    # `load_recent_macro_coverage`).
    persist_macro_coverage(
        session,
        report_id=report.id,
        user_id=user_id,
        as_of=datetime.fromisoformat(ctx.period_end) if ctx.period_end else datetime.now(tz=UTC),
        items=coverage_items,
    )
    # H-DEBT-3 (#30): mark this window's news as surfaced in the same
    # transaction as the status commit, so the two can never diverge.
    if news_items is not None:
        url_hashes = [item.url_hash for item in news_items]
    else:
        url_hashes = [
            item["url_hash"]
            for item in ctx.news_items
            if isinstance(item, dict) and isinstance(item.get("url_hash"), str)
        ]
        url_hashes.extend(
            item["url_hash"]
            for items in ctx.holding_news.values()
            for item in items
            if isinstance(item, dict) and isinstance(item.get("url_hash"), str)
        )
    url_hashes = list(dict.fromkeys(url_hashes))
    if extra_url_hashes:
        url_hashes = list(dict.fromkeys([*url_hashes, *extra_url_hashes]))
    mark_news_surfaced(session, user_id, report.id, url_hashes)
    session.commit()
    # issue #446: the "report-ready" milestone (Requirements point 2) —
    # committed status/body/coverage/news, i.e. the report exists as far as
    # any OTHER reader of the business Session is concerned. Deliberately
    # BEFORE email: email confirmation is a separate, independent outcome.
    if stage_state is not None:
        stage_state["persist_report"] = "ok"
    oe.end_span(_persist_span, "ok", attributes={"report_status": final_status})
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
    # flip an already-persisted success to 'failed'. Report success does NOT
    # imply email confirmation — a False/exception outcome here never changes
    # `report.status` (issue #446 Design §3).
    _email_span = oe.start_span("email_send")
    try:
        if send_report_email(report, session):
            if stage_state is not None:
                stage_state["email_send"] = "ok"
            oe.end_span(_email_span, "ok")
        else:
            # See the quiet-day branch above for why this no longer
            # claims delivery (PR #181 review).
            logger.warning(
                "report %s: email delivery not confirmed — see "
                "email_sender logs above for the cause",
                report.id,
            )
            if stage_state is not None:
                stage_state["email_send"] = "unconfirmed"
            oe.end_span(_email_span, "unconfirmed")
    except Exception:
        logger.exception("report %s: email send raised unexpectedly", report.id)
        if stage_state is not None:
            stage_state["email_send"] = "failed"
        oe.end_span(_email_span, "failed", reason_code="unexpected_exception")

    return report


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_report(
    session: Session,
    user_id: uuid.UUID,
    report_date: date | None = None,
    report_type: str = "incremental",
    base_currency: str = "USD",
    output_lang: str = "en",
    session_node: str = "manual",
    moves_cache: MovesCache | None = None,
    now: datetime | None = None,
    users_remaining: int = 1,
    _telemetry_attrs: dict[str, Any] | None = None,
) -> Report:
    """
    Run the full F1 report generation pipeline and persist the result.

    Returns the Report ORM object (status='success' or 'failed').
    Raises if the report record cannot be written (e.g. unique constraint violation
    when a report for the same date+type+session_node already exists).

    `user_id` (issue #129 B3): required, no ambient fallback — every caller
    (the on-demand `generate_report_job` task, which resolves it from the
    `report_jobs` row it was accepted as,
    `generate_incremental_report`'s multi-user fan-out, scripts) must
    resolve identity itself and pass it in explicitly.

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

    `_telemetry_attrs` (issue #446): private — batch-position attributes
    (`recipient_index`/`users_remaining`/`batch_offset_ms`) a fan-out caller
    (report_tasks.py) attaches to this attempt's operational-event span.
    `None` (every other call site) attaches nothing extra.

    `users_remaining` (issue #128 A4): how many users, INCLUDING this one,
    the current fan-out still has to serve. Forwarded to the L1/L2 shared
    caches so each user gets a fair slice of the day's remaining analysis
    budget instead of the first user in a never-rotating order spending it
    all — see `shared_budget.fair_share_budget` for why this same problem
    surfaced once per checkpoint. `1` (every non-fan-out call site: manual
    trigger, tests, a single-user system) means no restriction at all.
    """
    # issue #446: started before the idempotency lookup below (Design §3) —
    # inherits the caller's task run as a child span if one is active
    # (report_tasks.py), else becomes its own standalone root run (a direct
    # self-service/admin call). `stage_state` is a compact map of the named
    # stages this attempt actually reaches; unreached stages default to
    # "not_reached" and are attached to the root end event so a killed/failed
    # attempt's SQL trail shows what it never got to, not just what failed.
    _attempt = oe.start_attempt(
        "report.generate",
        user_id=user_id,
        attributes={
            "report_type": report_type,
            "session_node": session_node,
            **(_telemetry_attrs or {}),
        },
    )
    _stage_state: dict[str, str] = dict.fromkeys(
        (
            "preparation",
            "l2_intel",
            "l1_intel",
            "l3_synthesis",
            "assembly",
            "pass2_analysis",
            "shadow_assembly",
            "render_and_compliance",
            "persist_report",
            "email_send",
        ),
        "not_reached",
    )
    validate_report_type(report_type)
    settings = get_settings()
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
        oe.set_report_id(existing.id)
        if existing.status == "success" and existing.email_sent_at is None:
            # #61: the render/persist commit succeeded but the email attempt
            # after it either failed, was skipped, or never confirmed
            # delivery — send_report_email never flips status or raises, so
            # this row is otherwise unreachable by any retry. Resend using
            # the already-persisted body: no re-render, no LLM call.
            logger.info(
                "report %s: success but email never confirmed sent — resending only",
                existing.id,
            )
            if not send_report_email(existing, session):
                logger.warning(
                    "report %s: email delivery not confirmed — see "
                    "email_sender logs above for the cause",
                    existing.id,
                )
            oe.end_attempt(_attempt, "ok", attributes={"path": "email_only"})
            return existing
        logger.info(
            "report %s: already complete for %s (status=%s) — returning existing",
            existing.id,
            eff_date,
            existing.status,
        )
        oe.end_attempt(_attempt, "ok", attributes={"path": "noop"})
        return existing

    # ------------------------------------------------------------------
    # Create or reset report record (status=in_progress)
    # ------------------------------------------------------------------
    prior_ctx: ReportContext | None = None
    if existing is not None:
        report = existing
        # #61: a retry of a failed/in_progress row whose prior attempt
        # already produced a complete Pass 2/assembly body — under the SAME
        # prompt/disclaimer version this retry would otherwise use — can
        # skip re-running the body LLM call and resume
        # directly from render. Snapshot report_inputs/prompt_version/
        # disclaimer_version BEFORE the reset below would overwrite them.
        # needs_review is deliberately excluded (status check above only
        # short-circuits success/skipped, so needs_review still reaches
        # here): _render_full_md is a pure function of raw_body, so reusing
        # the same raw_body would reproduce the exact same compliance
        # violations — a needs_review retry only has a chance at different
        # output by redoing the body pass, so it always takes the full
        # reset path below.
        prior_inputs = cast(ReportInputsDict | None, existing.report_inputs)
        prior_stored_body = (
            (prior_inputs.get("assembly_raw") or prior_inputs.get("pass2_raw") or "")
            if prior_inputs
            else ""
        )
        reusable = bool(
            existing.status != "needs_review"
            and prior_stored_body
            and existing.prompt_version == _PROMPT_VERSION
            and existing.disclaimer_version == _DISCLAIMER_VERSION
        )
        if reusable:
            assert prior_inputs is not None
            prior_ctx = ReportContext.from_jsonb(cast(dict[str, Any], prior_inputs))
            report.status = "in_progress"
            # prompt_version/disclaimer_version already match (that's what
            # made this reusable) — report_md/report_inputs/generated_at/
            # email_sent_at/provider_message_id and the news window
            # (unmark_news_surfaced) stay untouched: the stage-skip branch
            # below (_finish_report) overwrites them with equivalent content
            # anyway, and unmarking would only matter for a re-fetch of the
            # news window, which the stage-skip branch never does.
        else:
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
        exclude_id = report.id if existing is not None else None
        period_start = user_watermark(
            session,
            user_id,
            report_type,
            exclude_report_id=exclude_id,
            now=now,
        )
        period_end = now
        report.period_start = period_start
        report.period_end = period_end
        if period_start == cold_start_watermark(now):
            backfill_news_surfaced_before(session, user_id, period_start)
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

    oe.set_report_id(report.id)

    if prior_ctx is not None:
        # #61: resume straight from render using the stored Pass 2/assembly
        # body — everything upstream of it (portfolio/news/anomalies fetch,
        # macro/L1/L2/cross-name intel, article reads, Pass 2/assembly) is
        # skipped entirely, not just the LLM calls, since prior_ctx already
        # carries every field the render step reads.
        logger.info(
            "report %s: resuming from stored Pass 2/assembly body — skipping body generation (#61)",
            report.id,
        )
        resume_raw_body = prior_ctx.assembly_raw or prior_ctx.pass2_raw
        try:
            _resumed = _finish_report(
                session,
                report,
                prior_ctx,
                user_id,
                eff_date,
                output_lang,
                resume_raw_body,
                None,
                stage_state=_stage_state,
            )
            oe.end_attempt(
                _attempt, "ok", attributes={"path": "resume_body", "stage_state": _stage_state}
            )
            return _resumed
        except Exception:
            logger.exception("report %s: generation failed (resume)", report.id)
            report.status = "failed"
            report.report_inputs = prior_ctx.to_jsonb()
            log_ops_event("report.generate.end", report_id=str(report.id), status="failed")
            oe.end_attempt(
                _attempt,
                "failed",
                attributes={"path": "resume_body", "stage_state": _stage_state},
            )
            try:
                session.commit()
            except Exception:
                session.rollback()
            raise

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
    # Recorded once, regardless of which pass ends up writing the body (§3.3(6)):
    # Pass 2 and assembly compose from the same framework text, so one version
    # covers both. Audit/reproducibility only — the framework's full text is
    # deliberately never stored here (see ReportContext.analysis_framework_version).
    ctx.analysis_framework_version = load_analysis_framework().version

    try:
        # ------------------------------------------------------------------
        # 1. Gather inputs (news + price moves read from the capture stores)
        # ------------------------------------------------------------------
        _preparation_span = oe.start_span("preparation")
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
                "report %s: %d holding(s) missing price, shown as unpriced in §1 "
                "and excluded from totals: %s",
                report.id,
                len(portfolio_snap.stale_tickers),
                stale_list,
            )
            alert_body = (
                f"Report {report.id} ({report.report_date}) could not price the "
                f"following holdings — they appear in §1 with a price-unavailable "
                f"placeholder and are excluded from all totals:\n\n"
                + "\n".join(f"  - {t}" for t in portfolio_snap.stale_tickers)
                + "\n\nCheck price_snapshots and capture logs."
            )
            send_ops_alert(
                subject=f"[Portfonia] price missing — {len(portfolio_snap.stale_tickers)} holding(s) unpriced",
                body=alert_body,
                idempotency_key=f"ops-price-missing-{report.id}",
                severity="WARNING",
            )
            create_bug_report(
                title=f"holdings unpriced: price missing for {stale_list}",
                body=(
                    f"## Holdings without a price in report\n\n"
                    f"**Report:** {report.id} ({report.report_date})\n\n"
                    f"**Unpriced holdings:** {stale_list}\n\n"
                    f"These holdings appear in §1 with a price-unavailable placeholder "
                    f"and are excluded from all aggregation totals. Likely causes: "
                    f"capture task failure, new holding with no price_snapshots row, "
                    f"or ticker/fund_code lookup mismatch.\n\n"
                    f"**Fix:** verify `price_snapshots` has recent rows for each "
                    f"identifier and that `compute_portfolio` looks them up correctly."
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
                severity="WARNING",
            )

        # FX stale check: if rates trail the window cutoff, valuation in non-USD
        # currencies is based on stale exchange rates — alert ops but don't block.
        # Per-currency since issue #354 — pairs now resolve independently, so
        # one currency's rate can be stale while another's is fine.
        fx_rates_as_of = ctx.portfolio_summary.get("fx_rates_as_of", {})
        stale_currencies = _fx_is_stale(fx_rates_as_of, period_end.isoformat())
        if stale_currencies:
            stale_list = ", ".join(
                f"{ccy} (as of {fx_rates_as_of[ccy]})" for ccy in stale_currencies
            )
            send_ops_alert(
                subject=f"[Portfonia] FX rates stale — report {report.report_date}",
                body=(
                    f"Report {report.id} ({report.report_date}): FX rates for the "
                    f"following currencies trail the window cutoff: {stale_list}. "
                    f"Portfolio values in these currencies and the FX footer note "
                    f"will reflect stale exchange rates.\n\n"
                    f"Likely cause: capture_fx_task missed or failed for these pairs. "
                    f"Check worker.log and run capture_fx_task.apply() to backfill."
                ),
                idempotency_key=f"ops-fx-stale-{report.id}",
                severity="WARNING",
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
        _stage_state["preparation"] = "ok"
        oe.end_span(_preparation_span, "ok")

        # ------------------------------------------------------------------
        # 2. Skip check
        # ------------------------------------------------------------------
        # issue #440 (owner decision 2026-09-12, "narrow/retire the quiet-path
        # shortcut"): a zero-keyword-hit report period is no longer, by
        # itself, treated as "no macro developments" — Requirements point 1.
        # This bypass now only fires when there is genuinely nothing to write
        # even a substantive overview from: no keyword theme hit, no price
        # anomaly, no window news at all (so no bounded-recovery candidate
        # exists either — Design §2 step 5's "collection/recall/filter check"
        # has nothing to work with), AND no eligible prior coverage this
        # reader is owed continuity on. In practice this condition is nearly
        # unreachable outside a genuinely empty capture window (news_items
        # empty is itself an ops-alertable capture-layer symptom elsewhere in
        # this pipeline) — that is the intended effect, not a bug: macro
        # coverage should almost never take the canned quiet path anymore.
        # Self-excluded (Contract constraints "no self/future-reference"): a
        # retry of this same report_id (needs_review always takes the full
        # reset path, never resume — see the retry branch above) may already
        # have its own prior coverage rows persisted from the attempt being
        # redone; those must never feed back into its own new prompt.
        ctx.macro_continuity_snapshot = load_recent_macro_coverage(
            session, user_id, exclude_report_id=report.id
        )
        has_macro_material = bool(
            macro_signals.has_any_hit or news_items or ctx.macro_continuity_snapshot
        )
        if not has_macro_material and not anomalies:
            logger.info("report %s: quiet day — no signals, no anomalies", report.id)
            # issue #440 (PR #441 review, blacktomb42): this branch now only
            # fires when there is genuinely nothing to work with — no
            # keyword hit, no window news, no eligible prior coverage to
            # revisit, AND no anomaly. The old "no macro keyword themes
            # triggered" copy was misleading framing once the gate stopped
            # meaning "quiet macro world"; say plainly that this window had
            # nothing at all to report from, not just that a keyword table
            # missed.
            quiet_body = (
                "## §2 Macro Events\n\n"
                "No macro developments to report this period: no keyword theme "
                "matched, no window news was captured, and no continuing topic "
                "was due for revisit.\n\n"
                "## §3 Holdings Context\n\n"
                "No significant market developments detected for monitored holdings.\n\n"
                "## §4 Exposure & Price Data\n\n"
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
            _stage_state["render_and_compliance"] = "ok"
            _stage_state["persist_report"] = "ok"
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
                oe.skip_span("email_send", reason_code="short_manual_quiet")
                _stage_state["email_send"] = "skipped"
                oe.end_attempt(
                    _attempt,
                    "ok",
                    attributes={"path": "quiet_day", "stage_state": _stage_state},
                )
                return report
            _email_span = oe.start_span("email_send")
            try:
                if send_report_email(report, session):
                    _stage_state["email_send"] = "ok"
                    oe.end_span(_email_span, "ok")
                else:
                    # False now covers more than "sent but the commit
                    # failed" — it's also send_report_email's fail-closed
                    # response to an unresolved recipient (issue #129 B3).
                    # Don't claim delivery either way; email_sender's own
                    # logging (ERROR on unresolved recipient, warning/
                    # exception on the specific transport failure) already
                    # states the real cause — PR #181 review.
                    logger.warning(
                        "report %s: email delivery not confirmed — see "
                        "email_sender logs above for the cause",
                        report.id,
                    )
                    _stage_state["email_send"] = "unconfirmed"
                    oe.end_span(_email_span, "unconfirmed")
            except Exception:
                logger.exception("report %s: quiet-day email send raised unexpectedly", report.id)
                _stage_state["email_send"] = "failed"
                oe.end_span(_email_span, "failed", reason_code="unexpected_exception")
            oe.end_attempt(
                _attempt, "ok", attributes={"path": "quiet_day", "stage_state": _stage_state}
            )
            return report

        # ------------------------------------------------------------------
        # 3-5. Read scheduled intelligence and prepare Pass 2 material.
        # ------------------------------------------------------------------
        trade_date = intel_trade_date(session, eff_date)
        ctx.intel_trade_date = trade_date.isoformat() if trade_date else ""
        ctx.search_queries = []
        ctx.search_results = (
            _load_report_articles(session, ctx, period_start, period_end) if trade_date else []
        )

        material_ids = list(
            dict.fromkeys(
                [
                    *[str(a["identifier"]) for a in ctx.price_anomalies if a.get("identifier")],
                    *large_weight_identifiers(
                        list(ctx.portfolio_summary.get("holdings") or []),
                        float(ctx.portfolio_summary.get("total_base") or 0.0),
                    ),
                ]
            )
        )
        headline_ids = _holding_headline_order(ctx)
        linked_news = load_instrument_news_by_identifier(
            session, period_start, period_end, user_id, headline_ids
        )
        recalled, recalled_hashes = _merge_holding_news(news_items, linked_news, headline_ids)
        recalled = {
            identifier: recalled[identifier]
            for identifier in headline_ids
            if identifier in recalled
        }
        recalled = dict(list(recalled.items())[:MAX_HOLDINGS_WITH_HEADLINES])
        ctx.holding_news = {
            identifier: _serialize_news(items) for identifier, items in recalled.items()
        }
        window_moves, _ = resolve_global_moves(session, period_start, period_end, moves_cache)
        ctx.large_holding_moves = {
            identifier: _serialize_holding_move(window_moves[identifier])
            for identifier in material_ids
            if identifier in window_moves
        }

        if trade_date is not None:
            l2_event_keys = l2_event_keys_for_user(session, trade_date, ctx.macro_signals)
            ctx.macro_event_intel = read_l2_intel(session, l2_event_keys, trade_date)
            ctx.macro_event_exposure = user_event_exposure(
                ctx.macro_event_intel, ctx.portfolio_summary.get("by_asset_class", {})
            )
            l1_identifiers = l1_identifiers_for_user(
                ctx.price_anomalies,
                holdings=list(ctx.portfolio_summary.get("holdings") or []),
                portfolio_total=float(ctx.portfolio_summary.get("total_base") or 0.0),
                exposed_asset_classes=sorted(
                    {cls for classes in ctx.macro_event_exposure.values() for cls in classes}
                ),
            )
            ctx.ticker_intel = read_l1_intel(session, l1_identifiers, trade_date)
            day_clusters = read_day_synthesis(session, trade_date)
            all_briefed = day_briefed_identifiers(session, trade_date)
            ctx.cross_name_intel = clusters_for_user(
                day_clusters, list(ctx.ticker_intel), all_briefed
            )
        _stage_state["l2_intel"] = "ok"
        _stage_state["l1_intel"] = "ok"
        _stage_state["l3_synthesis"] = "ok"

        # The current report body path reads scheduled intel and selects the
        # assembly or Pass 2 route below; this block owns that report path.
        if True:
            investor_prefs = load_investor_preferences(session, user_id)
            ctx.investor_questionnaire_snapshot = investor_prefs.questionnaire
            ctx.investor_questionnaire_version = investor_prefs.questionnaire_version
            client = _openrouter_client()
            _assembly_span = oe.start_span("assembly")
            raw_body = _try_assembly(client, settings, ctx, report.id, investor_prefs)
            if raw_body is not None:
                _stage_state["assembly"] = "ok"
                oe.end_span(_assembly_span, "ok")
                _stage_state["pass2_analysis"] = "skipped"
                oe.skip_span("pass2_analysis", reason_code="assembly_selected")
            else:
                _stage_state["assembly"] = "skipped"
                oe.end_span(_assembly_span, "skipped", reason_code="not_selected")
                _pass2_span = oe.start_span("pass2_analysis")
                pass2_user = _build_pass2_prompt(
                    ctx.portfolio_summary,
                    ctx.macro_signals,
                    ctx.price_anomalies,
                    ctx.search_results,
                    ctx.period_start,
                    ctx.period_end,
                    ctx.window_trading_days,
                    ctx.holding_news,
                    large_holding_moves=ctx.large_holding_moves,
                    investor_locale=investor_prefs.locale,
                    investor_questionnaire=investor_prefs.questionnaire,
                    investor_free_text=investor_prefs.free_text,
                    macro_continuity=ctx.macro_continuity_snapshot,
                )
                ctx.pass2_model = settings.PRIMARY_LLM_MODEL
                ctx.pass2_prompt = pass2_user
                raw_pass2 = _call_llm(
                    client,
                    settings.PRIMARY_LLM_MODEL,
                    _build_pass2_system(),
                    pass2_user,
                    with_holdings=True,
                    usage_sink=ctx.llm_calls,
                )
                if body_is_incomplete(raw_pass2):
                    ctx.rejected_pass2_raw = raw_pass2
                    oe.end_span(_pass2_span, "failed", reason_code="truncated_body")
                    raise RuntimeError(
                        f"report {report.id}: Pass 2 output looks truncated "
                        f"({len(raw_pass2)} chars, missing one of §3/§4)"
                    )
                ctx.pass2_raw = raw_pass2
                raw_body = raw_pass2
                _stage_state["pass2_analysis"] = "ok"
                oe.end_span(_pass2_span, "ok")
            _shadow_span = oe.start_span("shadow_assembly")
            try:
                _run_shadow_assembly(client, settings, ctx, report.id, investor_prefs)
                _stage_state["shadow_assembly"] = "ok"
                oe.end_span(_shadow_span, "ok")
            except Exception:
                logger.exception("report %s: shadow assembly harness failed", report.id)
                _stage_state["shadow_assembly"] = "degraded"
                oe.end_span(_shadow_span, "degraded", reason_code="harness_failed")
            result = _finish_report(
                session,
                report,
                ctx,
                user_id,
                eff_date,
                output_lang,
                raw_body,
                news_items,
                stage_state=_stage_state,
                extra_url_hashes=recalled_hashes,
            )
            oe.end_attempt(
                _attempt,
                "ok",
                attributes={
                    "path": "full",
                    "report_status": result.status,
                    "stage_state": _stage_state,
                },
            )
            return result

    except Exception as exc:
        logger.exception("report %s: generation failed", report.id)
        report.status = "failed"
        report.report_inputs = ctx.to_jsonb()
        log_ops_event("report.generate.end", report_id=str(report.id), status="failed")
        # issue #446: whichever named stage raised did not reach its own
        # `oe.end_span` call (a mid-stage exception unwinds straight past
        # it) — that stage's start row stays unmatched, which is the
        # designed "unknown/incomplete" signal (Design §3), not a bug to
        # paper over with a fabricated end here. The root attempt itself
        # still gets a clean, real end with whatever stage_state was
        # actually reached before the failure.
        oe.end_attempt(
            _attempt,
            "failed",
            reason_code=type(exc).__name__,
            attributes={"path": "full", "stage_state": _stage_state},
        )
        try:
            session.commit()
        except Exception:
            session.rollback()
        raise


def regenerate_report(
    session: Session,
    report_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    mode: str = "render",
    output_lang: str = "en",
    base_currency: str | None = None,
) -> Report:
    """Rebuild an existing report from its stored inputs WITHOUT re-fetching (#6).

    Intel acquisition (scheduled news and shared intel) is never repeated — that data is
    read back from `report_inputs`, so no token/credit is wasted on it.

    mode='render'  : zero new LLM cost except translation. Re-runs annotation +
                     assembly + language render from the stored Pass 2 body.
                     Use it to iterate on formatting/output language.
    mode='analyze' : re-runs only the body pass from the stored inputs (no
                     fetch or scheduled intel computation). Use it to iterate on the body
                     prompt. Which pass runs follows the report's own
                     `body_source` (A4): Pass 2 from the stored portfolio +
                     search results, or the assembly pass from the stored
                     L1/L2 intel. Updates that pass's stored body.

    `user_id` (issue #129 B3): required, no ambient fallback — the caller
    (the `/reports/{id}/regenerate` router via `Depends(current_principal)`)
    resolves identity itself. Used both to scope the ownership lookup below
    and, in mode='analyze', to re-fetch the live portfolio under the right
    user.

    `base_currency` (issue #350 item 1): only consulted in mode='analyze',
    where it overrides the ORIGINAL report's stored base_currency for the
    fresh live-portfolio refetch below — `None` (the default) preserves
    the pre-#350 behavior of re-using the stored value, so an existing
    caller that never passes this stays byte-for-byte unchanged. mode=
    'render' never reads this parameter: it has no live refetch to apply
    it to.

    Does not email — this is an iteration/inspection tool.
    """
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
        effective_base_ccy = base_currency if base_currency is not None else stored_base_ccy
        fresh_snap = compute_portfolio(
            session,
            user_id=user_id,
            base_currency=effective_base_ccy,
            as_of=report.period_end.astimezone(ET).date() if report.period_end else None,
        )
        portfolio = _serialize_portfolio(fresh_snap)
        regen_calls: list[dict[str, Any]] = []
        period_start_iso = report.period_start.isoformat() if report.period_start else ""
        period_end_iso = report.period_end.isoformat() if report.period_end else ""
        trading_days = int(inputs.get("window_trading_days", 0))

        # Re-fetched live, like fresh_technical below — a regenerate should
        # reflect a questionnaire answered/changed since the original
        # generation, not replay the stale answer set frozen in `inputs`
        # (issue #129 checkpoint B6). Loaded ONCE, before the body-source
        # branch, so the snapshot is set unconditionally regardless of which
        # pass re-runs (PR #212 review finding — the original implementation
        # only loaded this inside the Pass 2 branch, leaving an assembled
        # regenerate's snapshot stale/absent).
        investor_prefs = load_investor_preferences(session, user_id)

        # issue #440: re-fetched live like investor_prefs above, self-
        # excluding THIS report_id (Design §5 "Analyze regeneration excludes
        # the current report from historical context") — this report's own
        # PRIOR coverage rows (from the attempt being redone) must not feed
        # back into the prompt regenerating it.
        macro_continuity = load_recent_macro_coverage(session, user_id, exclude_report_id=report.id)

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
                investor_locale=investor_prefs.locale,
                investor_questionnaire=investor_prefs.questionnaire,
                investor_free_text=investor_prefs.free_text,
                macro_signals=inputs.get("macro_signals", {}),
                macro_continuity=macro_continuity,
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
                "analysis_framework_version": load_analysis_framework().version,
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
                "investor_questionnaire_snapshot": investor_prefs.questionnaire,
                "investor_questionnaire_version": investor_prefs.questionnaire_version,
                "macro_continuity_snapshot": macro_continuity,
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
                large_holding_moves=inputs.get("large_holding_moves", {}),
                investor_locale=investor_prefs.locale,
                investor_questionnaire=investor_prefs.questionnaire,
                investor_free_text=investor_prefs.free_text,
                macro_continuity=macro_continuity,
            )
            raw_body = _call_llm(
                _openrouter_client(),
                get_settings().PRIMARY_LLM_MODEL,
                _build_pass2_system(),
                pass2_user,
                with_holdings=True,
                usage_sink=regen_calls,
            )
            if body_is_incomplete(raw_body):
                raise RuntimeError(
                    f"report {report.id}: regenerated Pass 2 output looks truncated "
                    f"({len(raw_body)} chars, missing one of §3/§4)"
                )
            body_update = {
                "pass2_raw": raw_body,
                "pass2_prompt": pass2_user,
                "analysis_framework_version": load_analysis_framework().version,
                "investor_questionnaire_snapshot": investor_prefs.questionnaire,
                "investor_questionnaire_version": investor_prefs.questionnaire_version,
                "macro_continuity_snapshot": macro_continuity,
            }

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

    # This render/persist block is intentionally NOT `_finish_report()`
    # (generate_report's shared tail, #61/PR #341 review) — it must not
    # email or call `mark_news_surfaced`, and merges into the row's existing
    # `report_inputs` rather than a fresh `ctx.to_jsonb()`. See
    # `_finish_report`'s docstring for the full reasoning.
    #
    # issue #440: `raw_body` may carry the macro-coverage sidecar (present on
    # any body written under prompt f2-v10/a4-v6 or later; absent, and
    # harmlessly a no-op, on an older stored row — see extract_macro_sidecar's
    # docstring). Stripped here so it never reaches the rendered report under
    # EITHER mode.
    visible_body, coverage_items = extract_macro_sidecar(raw_body)
    report_date_str = report.report_date.strftime("%Y-%m-%d")
    label_hits: list[str] = []
    full_md, violations, translated_body = _render_full_md(
        report_date_str,
        portfolio,
        news_items,
        visible_body,
        output_lang,
        report.period_start.isoformat() if report.period_start else "",
        report.period_end.isoformat() if report.period_end else "",
        int(inputs.get("window_trading_days", 0)),
        inputs.get("price_anomalies", []),
        technical_positions,
        inputs.get("forward_events", []),
        str(inputs.get("price_data_through", "")),
        report_id=report.id,
        holding_news=inputs.get("holding_news", {}),
        label_hits=label_hits,
    )
    report.status = "needs_review" if violations else "success"
    report.report_md = full_md
    # Persist translation snapshot alongside report_md for compliance traceability.
    if report.report_inputs is not None:
        report.report_inputs = {
            **report.report_inputs,
            "pass2_translated": translated_body,
            "prompt_label_hits": label_hits,
        }
    report.generated_at = datetime.now(tz=UTC)
    if mode == "analyze":
        # Render-only regeneration re-runs no analysis (Design §5) and must
        # not touch this report's existing coverage rows — only `analyze`,
        # which just re-derived `coverage_items` from a FRESH body pass,
        # replaces them (delete-then-insert: Contract constraints
        # "Regenerate after a topic was removed: No stale coverage").
        persist_macro_coverage(
            session,
            report_id=report.id,
            user_id=user_id,
            as_of=report.period_end or datetime.now(tz=UTC),
            items=coverage_items,
        )
    session.commit()
    logger.info("report %s: regenerated (mode=%s, lang=%s)", report.id, mode, output_lang)
    return report
