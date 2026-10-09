"""Historical tail losses from the fixed five-year Jade replay (issue #718)."""

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from math import floor, sqrt
from statistics import NormalDist, mean, stdev
from typing import Literal, cast
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.user_investment_context import UserInvestmentContext
from app.schemas.jade import HistogramBin, JadeTailRiskOut, TailCell, TailLevel
from app.services.jade_replay import build_replay, ratio, valid_returns
from app.services.jade_replay_config import (
    HIST_BIN,
    MIN_RETURNS_99,
    MONTH_DAYS,
    TAIL_LEVELS,
    TRADING_DAYS,
)
from app.services.portfolio_risk import risk_answers_valid, risk_thresholds


def tail_count(n: int, level: int) -> int:
    return -(-n * (100 - level) // 100)


def historical(returns: list[float], level: int) -> tuple[float, float]:
    ordered = sorted(returns)
    k = tail_count(len(returns), level)
    return -ordered[k - 1], -mean(ordered[:k])


def month_returns(values: list[tuple[date, float]]) -> list[float]:
    return [values[i + MONTH_DAYS][1] / values[i][1] - 1 for i in range(len(values) - MONTH_DAYS)]


def normal(returns: list[float], level: int) -> tuple[float, float]:
    mu, sigma = mean(returns), stdev(returns)
    z = NormalDist().inv_cdf(level / 100)
    return z * sigma - mu, sigma * NormalDist().pdf(z) / (1 - level / 100) - mu


def reference(exceeds: Decimal, level: int) -> tuple[float, float]:
    z = NormalDist().inv_cdf(level / 100)
    sigma = float(exceeds)
    return z * sigma / sqrt(TRADING_DAYS), z * sigma * sqrt(MONTH_DAYS / TRADING_DAYS)


def histogram(returns: list[float]) -> list[HistogramBin]:
    counts: dict[int, int] = {}
    for r in returns:
        index = floor(Decimal(repr(r)) / HIST_BIN)
        counts[index] = counts.get(index, 0) + 1
    return [
        HistogramBin(
            lower=ratio(i * HIST_BIN), upper=ratio((i + 1) * HIST_BIN), count=counts.get(i, 0)
        )
        for i in range(min(counts), max(counts) + 1)
    ]


def cell(values: tuple[float, float], amount: Decimal | None = None) -> TailCell:
    var, cvar = map(ratio, values)

    def money(value: str) -> str | None:
        return (
            str((Decimal(value) * amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
            if amount is not None
            else None
        )

    return TailCell(var=var, cvar=cvar, var_amount=money(var), cvar_amount=money(cvar))


def compute_tail_risk(
    session: Session, user_id: UUID, base_currency: str, benchmark: str
) -> JadeTailRiskOut:
    b = build_replay(session, user_id, base_currency, benchmark, "5Y")
    context = session.get(UserInvestmentContext, user_id)
    answers = context.questionnaire if context is not None else None
    exceeds = None
    if risk_answers_valid(answers):
        assert answers is not None
        exceeds = risk_thresholds(
            str(answers["risk_appetite"]), str(answers["horizon"]), str(answers["objective"])
        )[1]
    out = JadeTailRiskOut(
        **{
            key: getattr(b.out, key)
            for key in (
                "status",
                "base_currency",
                "benchmark",
                "benchmark_symbol",
                "benchmark_name",
                "benchmark_status",
                "window_start",
                "window_end",
                "first_valid_date",
                "sample_count",
                "coverage",
            )
        },
        tolerance_status="ok" if exceeds is not None else "no_questionnaire",
        proxy_understates=Decimal(b.out.coverage.proxy_share)
        + Decimal(b.out.coverage.head_proxy_share)
        > 0,
        levels=[],
        histogram=[],
        portfolio_value=None,
        month_windows=0,
        month_independent=0,
    )
    if out.status != "ok":
        return out
    daily = [r for _, r in valid_returns(b.portfolio)]
    monthly = month_returns(b.portfolio)
    total = b.total.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    out.portfolio_value = str(total)
    out.month_windows = len(monthly)
    out.month_independent = (len(b.portfolio) - 1) // MONTH_DAYS
    benchmark_daily = (
        [r for _, r in valid_returns(b.benchmark)] if out.benchmark_status == "ok" else []
    )
    benchmark_monthly = month_returns(b.benchmark) if out.benchmark_status == "ok" else []
    for level in TAIL_LEVELS:
        available = level == 95 or len(daily) >= MIN_RETURNS_99
        bench_available = bool(benchmark_daily) and (
            level == 95 or len(benchmark_daily) >= MIN_RETURNS_99
        )
        ref = reference(exceeds, level) if exceeds is not None else None
        out.levels.append(
            TailLevel(
                level=cast(Literal[95, 99], level),
                available=available,
                daily=cell(historical(daily, level), total) if available else None,
                monthly=cell(historical(monthly, level), total) if available and monthly else None,
                normal_daily=cell(normal(daily, level)) if available else None,
                normal_monthly=cell(normal(monthly, level))
                if available and len(monthly) >= 2
                else None,
                tail_days=tail_count(len(daily), level) if available else None,
                tail_windows=tail_count(len(monthly), level) if available and monthly else None,
                benchmark_daily=cell(historical(benchmark_daily, level))
                if bench_available
                else None,
                benchmark_monthly=cell(historical(benchmark_monthly, level))
                if bench_available and benchmark_monthly
                else None,
                reference_daily=ratio(ref[0]) if ref else None,
                reference_monthly=ratio(ref[1]) if ref else None,
            )
        )
    out.histogram = histogram(daily)
    return out
