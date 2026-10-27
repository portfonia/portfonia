"""Technical position ratios are fractions, not percentage points."""

from datetime import date

from pydantic import BaseModel


class TechnicalPositionOut(BaseModel):
    ticker: str
    name: str
    last_close: float | None
    bars: int
    pct_vs_sma50: float | None
    pct_vs_sma200: float | None
    range_52w_low: float | None
    range_52w_high: float | None
    pct_in_52w_range: float | None
    vol_20d_annualized: float | None


class TechnicalTableOut(BaseModel):
    as_of: date
    holdings_date: date | None
    positions: list[TechnicalPositionOut]


class TechnicalPointOut(BaseModel):
    date: date
    close: float
    pct_vs_sma50: float | None
    pct_vs_sma200: float | None
    pct_in_52w_range: float | None
    vol_20d_annualized: float | None


class TechnicalSeriesOut(BaseModel):
    ticker: str
    name: str
    start: date
    end: date
    points: list[TechnicalPointOut]
