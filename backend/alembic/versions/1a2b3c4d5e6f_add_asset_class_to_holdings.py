"""add asset_class to holdings

Adds an economic-classification column separate from the LLM-parsed asset_type.

  STOCK          individual equity
  EQUITY_BROAD   broad index fund/ETF (S&P 500, Nasdaq 100, total market, etc.)
  EQUITY_REGION  single-country or regional fund (EWJ, FXI, …)
  EQUITY_SECTOR  single-sector fund
  COMMODITY      commodity fund or direct holding (gold, silver, oil, …)
  BOND_FUND      bond ETF or fund (BOXX, AGG, TLT, …)
  CASH_EQUIV     cash, money-market, margin, wealth-management products

Backfill order:
  1. Known tickers (hard-coded map below) override everything.
  2. Remaining rows are classified from asset_type as a fallback.
  3. Default is STOCK.

Revision ID: 1a2b3c4d5e6f
Revises: b8c9d0e1f2a3
Create Date: 2026-06-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TICKER_CLASS: dict[str, str] = {
    # Broad index funds / ETFs
    "QQQM": "EQUITY_BROAD",
    "QQQ": "EQUITY_BROAD",
    "VOO": "EQUITY_BROAD",
    "VTI": "EQUITY_BROAD",
    "SPY": "EQUITY_BROAD",
    "IVV": "EQUITY_BROAD",
    "513650": "EQUITY_BROAD",  # 华夏纳斯达克100ETF (A-share)
    # Country / region funds
    "EWJ": "EQUITY_REGION",
    "FXI": "EQUITY_REGION",
    "KWEB": "EQUITY_REGION",
    # Commodity funds
    "SGOL": "COMMODITY",
    "GLD": "COMMODITY",
    "IAU": "COMMODITY",
    "518660": "COMMODITY",  # 博时黄金ETF (A-share)
    "518800": "COMMODITY",  # 华安黄金ETF (A-share)
    # Bond / T-bill funds
    "BOXX": "BOND_FUND",
    "BIL": "BOND_FUND",
    "SHY": "BOND_FUND",
    "AGG": "BOND_FUND",
    "TLT": "BOND_FUND",
}


def upgrade() -> None:
    # Add with server_default so every existing row immediately gets 'STOCK'.
    op.add_column(
        "holdings",
        sa.Column(
            "asset_class",
            sa.Text(),
            nullable=False,
            server_default="STOCK",
        ),
    )

    # Override known tickers to their correct class.
    conn = op.get_bind()
    for ticker, cls in _TICKER_CLASS.items():
        conn.execute(
            sa.text("UPDATE holdings SET asset_class = :cls WHERE UPPER(ticker) = :ticker"),
            {"cls": cls, "ticker": ticker.upper()},
        )

    # Fallback: derive from asset_type for rows still at 'STOCK' that have a
    # known type.  (Ticker lookup above already set commodity/bond/region rows;
    # this covers cash/wmf that have no ticker.)
    conn.execute(
        sa.text(
            """
            UPDATE holdings
            SET asset_class = CASE asset_type
                WHEN 'cash' THEN 'CASH_EQUIV'
                WHEN 'wmf'  THEN 'CASH_EQUIV'
                ELSE asset_class
            END
            WHERE asset_type IN ('cash', 'wmf')
            """
        )
    )


def downgrade() -> None:
    op.drop_column("holdings", "asset_class")
