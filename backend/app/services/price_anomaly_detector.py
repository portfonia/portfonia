"""Detect significant single-day price movements (Ring 0 — Stage E3).

Thresholds (design §E3):
  stock  ±3 %
  etf    ±2 %
  fx     ±1 %   (checked against fx_rates table, not yfinance)

fund / cash / wmf holdings are skipped: funds have T+1 NAV lag and are
compared against manual-update values; cash/wmf have no daily price.
Manual-mode holdings are skipped regardless of asset type.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.services._yfinance import fetch_last_two_closes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_ASSET_THRESHOLDS: dict[str, Decimal] = {
    "STOCK":           Decimal("0.05"),
    "EQUITY_US_BROAD": Decimal("0.05"),
    "EQUITY_US_TECH":  Decimal("0.05"),
    "EQUITY_DM":       Decimal("0.05"),
    "EQUITY_CN":       Decimal("0.05"),
    "EQUITY_EM":       Decimal("0.05"),
    "EQUITY_BROAD":    Decimal("0.05"),  # catch-all fallback
    "EQUITY_REGION":   Decimal("0.04"),  # legacy; migrate to specific bucket
    "EQUITY_SECTOR":   Decimal("0.04"),
    "COMMODITY":       Decimal("0.04"),
    "BOND_FUND":       Decimal("0.02"),
    # CASH_EQUIV excluded — no exchange-priced daily quote
}
_FX_THRESHOLD = Decimal("0.01")

_RATIO = Decimal("0.0001")  # 4 dp for pct_change


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


# ---------------------------------------------------------------------------
# FX helpers
# ---------------------------------------------------------------------------


def _load_fx_last_two_dates(session: Session) -> tuple[date, date] | None:
    """Return (latest_date, prev_date) from fx_rates, or None if < 2 dates exist."""
    dates: list[date] = list(
        session.execute(
            select(FxRate.rate_date).distinct().order_by(FxRate.rate_date.desc()).limit(2)
        ).scalars()
    )
    if len(dates) < 2:
        return None
    return dates[0], dates[1]


def _load_fx_for_date(session: Session, rate_date: date) -> dict[str, Decimal]:
    return {
        row.pair: row.rate
        for row in session.execute(select(FxRate).where(FxRate.rate_date == rate_date)).scalars()
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_price_anomalies(session: Session) -> list[PriceAnomaly]:
    """
    Scan holdings and FX rates for significant single-day price movements.

    Holdings: fetches today's and yesterday's close from yfinance for all
    auto-mode stock/etf holdings; compares against per-asset-type thresholds.

    FX: reads the two most recent rows per pair from the fx_rates table
    (already populated by fx_fetcher); compares against the FX threshold.

    Returns anomalies sorted by |pct_change| descending (largest move first).
    """
    anomalies: list[PriceAnomaly] = []

    # ------------------------------------------------------------------
    # Holdings — stock and ETF
    # ------------------------------------------------------------------
    eligible_classes = set(_ASSET_THRESHOLDS)
    rows: list[Holding] = list(
        session.execute(
            select(Holding).where(
                Holding.pricing_mode == "auto",
                Holding.ticker.isnot(None),
                Holding.asset_class.in_(eligible_classes),
            )
        ).scalars()
    )

    if rows:
        unique_tickers = list({h.ticker for h in rows if h.ticker})
        two_closes = fetch_last_two_closes(unique_tickers)

        for h in rows:
            ticker = h.ticker
            assert ticker is not None
            closes = two_closes.get(ticker)
            if closes is None:
                logger.debug("no price data for %s — skipping anomaly check", ticker)
                continue
            current_pt, prev_pt = closes
            if prev_pt is None:
                logger.debug("no previous close for %s — skipping anomaly check", ticker)
                continue

            current = Decimal(str(current_pt[0]))
            prev = Decimal(str(prev_pt[0]))
            if prev == 0:
                continue

            pct = ((current - prev) / prev).quantize(_RATIO, rounding=ROUND_HALF_UP)
            threshold = _ASSET_THRESHOLDS[h.asset_class]

            if abs(pct) >= threshold:
                anomalies.append(
                    PriceAnomaly(
                        name=h.name,
                        identifier=ticker,
                        asset_type=h.asset_class,
                        current_price=current,
                        prev_price=prev,
                        pct_change=pct,
                        threshold=threshold,
                    )
                )
                logger.info(
                    "price anomaly: %s (%s) %+.2f%% (threshold ±%.0f%%)",
                    h.name,
                    ticker,
                    float(pct) * 100,
                    float(threshold) * 100,
                )

    # ------------------------------------------------------------------
    # FX rates (from DB — no extra yfinance call needed)
    # ------------------------------------------------------------------
    date_pair = _load_fx_last_two_dates(session)
    if date_pair:
        today_date, prev_date = date_pair
        today_fx = _load_fx_for_date(session, today_date)
        prev_fx = _load_fx_for_date(session, prev_date)

        for pair, current in today_fx.items():
            prev_maybe = prev_fx.get(pair)
            if prev_maybe is None or prev_maybe == 0:
                continue
            prev = prev_maybe
            pct = ((current - prev) / prev).quantize(_RATIO, rounding=ROUND_HALF_UP)
            if abs(pct) >= _FX_THRESHOLD:
                anomalies.append(
                    PriceAnomaly(
                        name=pair,
                        identifier=pair,
                        asset_type="fx",
                        current_price=current,
                        prev_price=prev,
                        pct_change=pct,
                        threshold=_FX_THRESHOLD,
                    )
                )
                logger.info(
                    "FX anomaly: %s %+.3f%% (threshold ±%.0f%%)",
                    pair,
                    float(pct) * 100,
                    float(_FX_THRESHOLD) * 100,
                )

    anomalies.sort(key=lambda a: abs(a.pct_change), reverse=True)
    logger.info("detect_price_anomalies: %d anomaly(s) found", len(anomalies))
    return anomalies
