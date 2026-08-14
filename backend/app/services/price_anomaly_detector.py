"""Shared price-anomaly data model.

``PriceAnomaly``/``ConstituentMove`` are populated by
``window_data.detect_window_anomalies`` (the live ADR-002 anomaly path) and
consumed by ``report_generator.py``. The last-two-closes detection function
that used to live in this module (pre-ADR-002) was removed as dead code —
see GitHub issue #36.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ConstituentMove:
    """One holding's contribution to a theme-level anomaly."""

    name: str
    identifier: str  # ticker
    pct_change: Decimal  # window net pct for this holding
    current_value: Decimal  # used for weighting; 0 if unknown


@dataclass
class PriceAnomaly:
    """A holding (or theme group) whose price moved beyond its threshold.

    The base fields describe the headline move (``pct_change`` vs ``threshold``).

    The window fields (all optional, populated only by ``detect_window_anomalies``)
    add the incremental-report detail: how the move split between the cumulative
    window drift and the single worst trading day, plus a session-by-session arc
    of the most recent trading day so the report can state *what was compared to
    what* (prior close → open → intraday range → close → after-hours) instead of
    a bare net percentage.

    When multiple holdings share a theme (e.g. SGOL + 518660 both tracking gold),
    ``detect_window_anomalies`` merges them into one anomaly entry.  ``name``
    becomes the theme label, ``identifier`` the theme key, and ``constituents``
    lists the per-holding breakdown.  The session arc is taken from the
    value-dominant constituent.
    """

    name: str  # holding name, theme label, or FX pair
    identifier: str  # ticker, theme key, or FX pair
    asset_type: str  # asset_class value or "fx"
    current_price: Decimal
    prev_price: Decimal
    pct_change: Decimal  # signed; +0.05 = +5 %, -0.04 = -4 %
    threshold: Decimal  # the breach threshold
    # --- window detail (incremental report only) ---
    trigger: str = "single_day"  # "single_day" | "cumulative"
    market: str = ""
    baseline_date: date | None = None  # close used as the window baseline
    latest_date: date | None = None  # most recent close in the window
    window_net_pct: Decimal | None = None  # baseline close → latest close
    max_day_pct: Decimal | None = None  # largest single-day move in the window (signed)
    max_day_date: date | None = None  # the trading day of that move
    # Most-recent-trading-day session arc (None where the node was not captured).
    prev_close: Decimal | None = None  # previous trading day's close
    day_open: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    day_close: Decimal | None = None
    after_hours: Decimal | None = None  # post-close last, if captured
    # --- theme aggregation (populated when this entry represents multiple holdings) ---
    theme: str | None = None  # e.g. "gold", "nasdaq_100"
    theme_label_zh: str | None = None
    theme_label_en: str | None = None
    constituents: list[ConstituentMove] = field(default_factory=list)
