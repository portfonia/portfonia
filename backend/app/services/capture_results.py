"""Immutable capture outcomes for NAV/ETF fallback orchestration (#389)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

NavSource = Literal["eastmoney", "sina"]
NavFetchError = Literal["transport", "http", "parse", "provider_error"]
CaptureKind = Literal["nav", "etf_close"]

PRICE_AGREE_ABS = Decimal("0.000001")
PRICE_AGREE_REL = Decimal("0.000001")


def is_positive_finite(value: Decimal) -> bool:
    return value.is_finite() and value > 0


def prices_agree(left: Decimal, right: Decimal) -> bool:
    if not is_positive_finite(left) or not is_positive_finite(right):
        return False
    return abs(left - right) <= max(PRICE_AGREE_ABS, abs(right) * PRICE_AGREE_REL)


@dataclass(frozen=True)
class NavPoint:
    nav_date: date
    unit_nav: Decimal
    source: NavSource


@dataclass(frozen=True)
class NavHistoryOutcome:
    points: tuple[NavPoint, ...]
    error: NavFetchError | None = None


@dataclass(frozen=True)
class CaptureTarget:
    key: str
    market: str
    kind: CaptureKind
    reason: str
    cutoff: date | None = None
    latest_date: date | None = None
    missing_dates: tuple[date, ...] = ()
    window_start: date | None = None
    window_end: date | None = None


@dataclass(frozen=True)
class CaptureOutcome:
    written: int
    unresolved: tuple[CaptureTarget, ...]
    recovered: tuple[str, ...]
    history_coverage: dict[str, str]


class CaptureDataMiss(Exception):
    """Terminal data miss after the bounded retry/fallback budget."""

    def __init__(self, message: str, outcome: CaptureOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome
