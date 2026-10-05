"""`report_inputs` JSONB shape: the write-side dataclass and its read-side
TypedDict mirror.

Split out of report_generator.py (#37) so the type can be imported by other
report_* modules without them depending on the orchestrator.

Keys written by the removed L1/L2/L3, assembly and report-time search paths
(issue #640) may still appear in old stored rows; `from_jsonb` ignores them.
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
    search_results: list[dict[str, Any]]
    intel_trade_date: str
    pass2_model: str
    pass2_prompt: str
    pass2_raw: str
    rejected_pass2_raw: str
    llm_calls: list[dict[str, Any]]
    pass2_translated: str
    prompt_label_hits: list[str]
    body_source: str
    analysis_framework_version: str
    # B6 audit snapshot (issue #129, Ring 1-B design.md §8.4) — the full
    # closed-enum questionnaire answers actually used for THIS report, not
    # just the two keys (locale/intel_focus) that reached the prompt. free_text
    # is deliberately excluded (see investment_context.py's
    # InvestorPreferences docstring) — report_inputs is unencrypted JSONB.
    investor_questionnaire_snapshot: dict[str, Any] | None
    investor_questionnaire_version: str | None
    # issue #440: the MACRO COVERAGE CONTINUITY candidates actually offered
    # to this report's §2-writing prompt (see macro_coverage.py). Audit/
    # reproducibility snapshot, same rationale as
    # investor_questionnaire_snapshot above — read live from `macro_coverage`
    # on every generate/regenerate(analyze) call, not replayed from a prior
    # report's stored value.
    macro_continuity_snapshot: list[dict[str, Any]]


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
    search_results: list[dict[str, Any]] = field(default_factory=list)
    intel_trade_date: str = ""
    pass2_model: str = ""
    pass2_prompt: str = ""
    pass2_raw: str = ""
    # Output rejected by the completeness guard, written only on the failure
    # path. Must never be read as a report body.
    rejected_pass2_raw: str = ""
    # LLM call records (Pass 2; translation chunks excluded as they are
    # cheap/many and the per-chunk token count is not material for cost audits).
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    # Snapshot of the translated report body (dynamic section only, pre-footer).
    # Stored so compliance attribution can be traced per translation chunk if needed.
    pass2_translated: str = ""
    # Which pass wrote the shipped body. Always "pass2" since issue #640
    # removed the assembly path; kept so stored rows stay self-describing.
    body_source: str = "pass2"
    # System default analysis framework version (issue #128 Ring 1 stage B,
    # checkpoint B1 — config/analysis_framework.yml's own `version` field,
    # NOT the full framework text: audit/reproducibility only, kept out of
    # report_inputs to avoid the text ever being incidentally exposed
    # through a future endpoint that reads this column).
    analysis_framework_version: str = ""
    # See ReportInputsDict above for the field-by-field rationale.
    investor_questionnaire_snapshot: dict[str, Any] | None = None
    investor_questionnaire_version: str | None = None
    # See ReportInputsDict above for the field-by-field rationale.
    macro_continuity_snapshot: list[dict[str, Any]] = field(default_factory=list)

    prompt_label_hits: list[str] = field(default_factory=list)

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
        render/translate/persist from a prior attempt's completed Pass 2
        output without recomputing anything upstream of it. Unknown
        keys (a JSONB written by a newer field than this dataclass has, or an
        older row missing a since-added field — see ReportInputsDict's
        `total=False` note) are ignored/defaulted via plain dataclass
        construction rather than raising.
        """
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})
