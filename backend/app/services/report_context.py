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
from dataclasses import asdict, dataclass, field
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
