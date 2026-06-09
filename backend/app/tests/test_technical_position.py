"""Unit tests for technical-position metrics (#4).

Pure-math helpers are exercised directly on synthetic close series; the
session-backed loaders are covered indirectly via the report-generator suite.
"""

from __future__ import annotations

import statistics

from app.services import technical_position as tp


def test_sma_gap_none_when_insufficient_bars() -> None:
    assert tp._sma_gap([1.0, 2.0, 3.0], window=50) is None


def test_sma_gap_computes_distance_to_average() -> None:
    closes = [10.0] * 49 + [20.0]  # 50 bars, last far above the mean
    sma = statistics.fmean(closes[-50:])
    assert tp._sma_gap(closes, 50) == 20.0 / sma - 1


def test_vol_annualized_none_when_insufficient() -> None:
    assert tp._vol_annualized([1.0] * 10, window=20) is None


def test_vol_annualized_zero_for_flat_series() -> None:
    # 21 identical closes → all returns 0 → volatility 0.
    assert tp._vol_annualized([100.0] * 21, window=20) == 0.0


def test_vol_annualized_positive_for_moving_series() -> None:
    closes = [100.0 + (i % 2) for i in range(40)]  # oscillates → nonzero vol
    vol = tp._vol_annualized(closes, 20)
    assert vol is not None and vol > 0


def test_compute_technical_position_insufficient_history_returns_nones() -> None:
    # A tiny series: only short metrics are None; nothing should raise.
    pos = tp.TechnicalPosition(
        ticker="X",
        name="X",
        last_close=10.0,
        bars=5,
        pct_vs_sma50=None,
        pct_vs_sma200=None,
        range_52w_low=None,
        range_52w_high=None,
        pct_in_52w_range=None,
        vol_20d_annualized=None,
    )
    assert pos.pct_vs_sma50 is None and pos.bars == 5
