"""`report_inputs` JSONB shape: the write-side dataclass and its read-side
TypedDict mirror.

Split out of report_generator.py (#37) so the type can be imported by other
report_* modules without them depending on the orchestrator — e.g.
report_search.py's `_targeted_anomaly_queries` reads anomaly dicts shaped by
this module. `_tavily_used_today` (issue #128 A2) no longer reads
report_inputs at all — it counts `search_cache` rows directly, since a query
that hit cache made no real API call but was still being counted as spend
under the old report_inputs-summation approach.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypedDict


def _decimal_default(o: object) -> object:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"not JSON-serialisable: {type(o)}")


class ReportInputsDict(TypedDict, total=False):
    """Static-typed view of the `report_inputs` JSONB shape (#39).

    Mirrors ReportContext's field set — keep the two in sync by hand; the
    to_jsonb() JSON round-trip (dataclasses.asdict -> json.dumps/loads) means
    nothing enforces that sync automatically, only mypy's key/type checking
    at call sites that `cast` a raw dict into this type.

    `total=False`: every key is optional at read time. Rows written before a
    later ReportContext field was added won't have it, and
    regenerate_report's analyze-mode update (`{**inputs, "pass2_raw": ...}`)
    only ever adds/overwrites the keys it touches, never re-derives the rest
    — so no key here can be assumed universally present on a stored row.
    """

    portfolio_summary: dict[str, Any]
    news_items: list[dict[str, Any]]
    macro_signals: dict[str, Any]
    price_anomalies: list[dict[str, Any]]
    technical_positions: list[dict[str, Any]]
    forward_events: list[dict[str, Any]]
    holding_news: dict[str, list[dict[str, Any]]]
    large_holding_moves: dict[str, dict[str, Any]]
    period_start: str
    period_end: str
    window_trading_days: int
    price_data_through: str
    pass1_model: str
    pass1_prompt: str
    pass1_raw: str
    search_queries: list[str]
    search_results: list[dict[str, Any]]
    pass2_model: str
    pass2_prompt: str
    pass2_raw: str
    llm_calls: list[dict[str, Any]]
    pass2_translated: str
    ticker_intel: dict[str, str]
    macro_event_intel: dict[str, dict[str, Any]]
    macro_event_exposure: dict[str, list[str]]
    cross_name_intel: list[dict[str, Any]]
    body_source: str
    assembly_model: str
    assembly_prompt: str
    assembly_raw: str
    assembly_prompt_version: str
    assembly_shadow: dict[str, dict[str, Any]]
    analysis_framework_version: str
    # B6 audit snapshot (issue #129, Ring 1-B design.md §8.4) — the full
    # closed-enum questionnaire answers actually used for THIS report, not
    # just the two keys (locale/intel_focus) that reached the prompt. free_text
    # is deliberately excluded (see investment_context.py's
    # InvestorPreferences docstring) — report_inputs is unencrypted JSONB.
    investor_questionnaire_snapshot: dict[str, Any] | None
    investor_questionnaire_version: str | None


@dataclass
class ReportContext:
    """Intermediate documents captured for the report_inputs JSONB column."""

    portfolio_summary: dict[str, Any] = field(default_factory=dict)
    news_items: list[dict[str, Any]] = field(default_factory=list)
    macro_signals: dict[str, Any] = field(default_factory=dict)
    price_anomalies: list[dict[str, Any]] = field(default_factory=list)
    technical_positions: list[dict[str, Any]] = field(default_factory=list)
    forward_events: list[dict[str, Any]] = field(default_factory=list)
    # R-3 holding-relevant news: {identifier: [news dict, ...]} recalled from the
    # window store for the holdings that moved (plus any targeted-search items).
    holding_news: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Window price move for a large-weight holding that never crossed this
    # window's anomaly threshold (issue #128 narrative-layer redesign,
    # 2026-08-20 design amendment "make Pass 2 write the connection again, not
    # just name it", item 3). Without this, a holding like TSM
    # (weight-selected into `large_weight_identifiers`, but not an anomaly)
    # had ZERO price fact in Pass 2's prompt — DIRECTION REQUIRES EVIDENCE
    # then forced the body to drop the holding's own window move entirely
    # rather than state the real, unremarkable number. Anomaly holdings are
    # excluded (they already have a PRICE ANOMALIES row); this is strictly
    # the below-threshold set.
    #
    # {identifier: {"net_pct": float, "max_day_pct": float | None,
    # "max_day_date": str | None}} — net and max-day are two SEPARATE facts
    # (2026-08-20 second design amendment, item 3), not merged into one
    # number: the v6 compare fed only net_pct, and the body conflated it with
    # the window's largest single-day move in prose (TSM's window net was a
    # quiet +0.11%, but the window also contained a real +1.22% single day —
    # a reader cannot recover that distinction from one blended figure).
    large_holding_moves: dict[str, dict[str, Any]] = field(default_factory=dict)
    # ADR-002 window bookkeeping (ISO strings / int) for re-render reproducibility.
    period_start: str = ""
    period_end: str = ""
    window_trading_days: int = 0
    # R-5: ISO date of the last in-window close (the real PRICE-data cutoff,
    # distinct from period_end). Empty when the window has no captured close.
    price_data_through: str = ""
    pass1_model: str = ""
    pass1_prompt: str = ""
    pass1_raw: str = ""
    search_queries: list[str] = field(default_factory=list)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    pass2_model: str = ""
    pass2_prompt: str = ""
    pass2_raw: str = ""
    # LLM call records (Pass 1 + Pass 2 + L1 shared-intel analyses, issue
    # #128 A2 — `get_l1_intel_batch(..., usage_sink=ctx.llm_calls)`;
    # translation chunks excluded as they are cheap/many and the per-chunk
    # token count is not material for cost audits).
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    # Snapshot of the translated report body (dynamic section only, pre-footer).
    # Stored so compliance attribution can be traced per translation chunk if needed.
    pass2_translated: str = ""
    # L1 shared ticker intel (issue #128 A2): {identifier: cached analysis}
    # for the identifiers that triggered an anomaly or a holding-news recall
    # this window. NOT consumed by the Pass 2 prompt or the rendered body yet
    # — A2 only seeds/reads the shared cache (design doc §1.2: report content
    # stays byte-identical through A1-A3); A4 is what assembles this into the
    # report. Stored here for audit and so a future A4 read-back has it
    # without a DB re-query.
    ticker_intel: dict[str, str] = field(default_factory=dict)
    # L2 shared macro-event intel (issue #128 A3): {event_key: {analysis,
    # affected_asset_classes, affected_sectors}} for the macro themes this
    # user's own signals hit plus the day's forward-calendar events. The
    # values are SHARED across every user who touched the same event that
    # day (see macro_event_intel.py); only which keys appear here is
    # per-user.
    macro_event_intel: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The per-user half of L2: {event_key: [asset_class, ...]} — the cached
    # affected classes intersected with THIS user's own by_asset_class keys.
    # Pure set arithmetic, no LLM call. Like `ticker_intel`, neither field
    # feeds Pass 2 or the rendered body yet — A4 is the consumer (design doc
    # §1.2/§6.3); both are stored now so A4 reads them back without a
    # re-query and so an audit can see what the shared layer produced.
    macro_event_exposure: dict[str, list[str]] = field(default_factory=dict)
    # L3 day-level cross-identifier synthesis (issue #128 quality gate),
    # already narrowed to this user: [{identifiers, mechanism, summary,
    # confidence}]. The stored clusters are GLOBAL (one inference per trading
    # day for the whole system — see cross_name_intel.py); what makes this
    # field per-user is `clusters_for_user`, which intersects each cluster's
    # identifiers with this user's own L1 keys and drops what is left too
    # small. Same selection/values split as `ticker_intel` above, one layer
    # up: selection per-user, values global.
    cross_name_intel: list[dict[str, Any]] = field(default_factory=list)
    # --- A4 personalized assembly (issue #128) -----------------------------
    # Which pass actually wrote the shipped body: "pass2" (pre-A4 behavior,
    # and every fallback path) or "assembly". Recorded rather than inferred,
    # so a stored row states plainly which architecture produced it — and so
    # regenerate_report knows which pass to re-run in analyze mode.
    body_source: str = "pass2"
    assembly_model: str = ""
    assembly_prompt: str = ""
    # The assembled §2/§3/§4 body. Populated ONLY when the assembly pass
    # produced the shipped body; a fallback to Pass 2 leaves it empty, so
    # `assembly_raw or pass2_raw` is an unambiguous "the body that shipped".
    assembly_raw: str = ""
    assembly_prompt_version: str = ""
    # Shadow comparison (design doc §6.3.1): {model: {prompt, raw, error}}.
    # Never rendered, never emailed — read side by side by the product owner
    # against the shipped body, with costs in `llm_calls`.
    assembly_shadow: dict[str, dict[str, Any]] = field(default_factory=dict)
    # System default analysis framework version (issue #128 Ring 1 stage B,
    # checkpoint B1 — config/analysis_framework.yml's own `version` field,
    # NOT the full framework text: audit/reproducibility only, kept out of
    # report_inputs to avoid the text ever being incidentally exposed
    # through a future endpoint that reads this column). Same version
    # regardless of body_source — both Pass 2 and assembly compose from the
    # same framework text (§3.3(3)).
    analysis_framework_version: str = ""
    # See ReportInputsDict above for the field-by-field rationale.
    investor_questionnaire_snapshot: dict[str, Any] | None = None
    investor_questionnaire_version: str | None = None

    def to_jsonb(self) -> dict[str, Any]:
        """Return the write-side dict for the `report_inputs` JSONB column.

        Kept as `dict[str, Any]`, not `ReportInputsDict` (#39) — the column
        itself is untyped JSONB (`Mapped[dict[str, Any] | None]` on the ORM
        model), so a TypedDict return here would only fight that boundary at
        every assignment site for no type-safety gain. `ReportInputsDict` is
        the read-side contract instead: callers `cast` into it once they pull
        a row's `report_inputs` back out, which is where the drift this issue
        cares about (readers assuming a key/shape ReportContext never wrote)
        actually gets caught.
        """
        result: dict[str, Any] = json.loads(json.dumps(asdict(self), default=_decimal_default))
        return result

    @classmethod
    def from_jsonb(cls, data: dict[str, Any]) -> ReportContext:
        """Rehydrate a ReportContext from a previously stored `report_inputs`.

        Used by generate_report's stage-skip-on-retry path (#61) to resume
        render/translate/persist from a prior attempt's completed Pass 2 or
        assembly output without recomputing anything upstream of it. Unknown
        keys (a JSONB written by a newer field than this dataclass has, or an
        older row missing a since-added field — see ReportInputsDict's
        `total=False` note) are ignored/defaulted via plain dataclass
        construction rather than raising.
        """
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})
