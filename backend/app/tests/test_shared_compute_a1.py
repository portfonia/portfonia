"""Shared fixtures retained for weekly fan-out tests.

The former report-side shared-compute UATs were removed by #622; scheduled
intel behavior is covered by the slot-worker tests.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.portfolio_calculator import Concentration, PortfolioSnapshot


def _empty_portfolio_snap() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        base_currency="USD",
        holdings=[],
        total_base=Decimal("0"),
        by_currency={},
        by_asset_type={},
        by_market={},
        by_asset_class={},
        concentration=Concentration(
            top_holding_name="",
            top_holding_ratio=Decimal("0"),
            top_holding_asset_class="",
            top3_ratio=Decimal("0"),
            top_asset_class_name="",
            top_asset_class_ratio=Decimal("0"),
            single_holding_watch=False,
            single_holding_high=False,
            top3_watch=False,
            asset_class_watch=False,
            asset_class_high=False,
        ),
        stale_tickers=[],
    )
