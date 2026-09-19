"""Capture-health probe (issue #372 slice B; weekend split issue #487).

Alert rule: 21:30 ET every calendar day. FX/price/benchmark expected date
is the last Mon-Fri on or before `as_of` (a non-trading day never produces
a fresh close/rate). Portfolio expected date is `as_of` itself — weekends
are legitimate snapshot days. 36h wall-clock is not used — a Monday probe
vs Friday market evidence is 72h and would false-positive every weekend.

Hard-fail (retries exhausted) stays on capture_tasks._capture_failed.
This module only notices lag and non-complete portfolio batches.

Does not write snapshots, invent days, or replay an outbox (#373).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.alert_dedup import already_alerted, mark_alerted
from app.core.config import get_settings
from app.core.timezones import ET
from app.models.benchmark_price import BenchmarkPrice
from app.models.fx_rate import FxRate
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.price_snapshot import PriceSnapshot
from app.services.email_sender import send_ops_alert

logger = logging.getLogger(__name__)

_ALERT_DEDUP_TTL_SECONDS = 7 * 24 * 60 * 60


def expected_capture_date(as_of: date) -> date:
    """Last Mon-Fri calendar date on or before `as_of` (no holiday calendar)."""
    while as_of.weekday() >= 5:
        as_of -= timedelta(days=1)
    return as_of


def expected_capture_date_portfolio(as_of: date) -> date:
    """Portfolio-snapshot pipeline expects fresh evidence every calendar
    day — unlike market-data pipelines, a non-trading day is still a
    legitimate capture target (carried-forward marks, D5 / Requirement 3)."""
    return as_of


def is_stale(last_success: date | None, expected: date) -> bool:
    return last_success is None or last_success < expected


def _is_repeat(last: date | None, expected: date) -> bool:
    """True if this pipeline was ALSO stale on the immediately preceding
    trading day's own probe — reuses expected_capture_date so a Monday
    probe correctly checks against the prior Friday, not calendar-Sunday."""
    prior_expected = expected_capture_date(expected - timedelta(days=1))
    return last is None or last < prior_expected


def should_alert_portfolio(
    last_complete: date | None,
    expected: date,
    skipped_deps: int,
    pending: int,
) -> bool:
    return is_stale(last_complete, expected) or skipped_deps > 0 or pending > 0


@dataclass(frozen=True)
class CaptureHealthReport:
    expected_date: date
    issues: tuple[str, ...]
    skipped_deps: int
    pending: int
    # issue #426: each pipeline's own last-success date, so the alert body
    # can say how stale a flagged pipeline actually is instead of just
    # naming it. `evaluate_capture_health` already computes all four; this
    # carries them past the point they were previously discarded.
    price_last: date | None = None
    fx_last: date | None = None
    bench_last: date | None = None
    complete_last: date | None = None
    # Last Mon-Fri on or before the probe. Market-data wording and
    # repeat-detection use this; `expected_date` is the probe/portfolio
    # calendar day (issue #487).
    market_expected_date: date | None = None

    def market_day(self) -> date:
        return self.market_expected_date or expected_capture_date(self.expected_date)

    def should_alert(self) -> bool:
        return bool(self.issues)

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_date": self.expected_date.isoformat(),
            "issues": list(self.issues),
            "skipped_deps": self.skipped_deps,
            "pending": self.pending,
            "price_last": self.price_last.isoformat() if self.price_last else None,
            "fx_last": self.fx_last.isoformat() if self.fx_last else None,
            "bench_last": self.bench_last.isoformat() if self.bench_last else None,
            "complete_last": self.complete_last.isoformat() if self.complete_last else None,
            "market_expected_date": self.market_day().isoformat(),
        }


def _max_date(session: Session, column: object) -> date | None:
    value = session.execute(select(func.max(column))).scalar_one()
    return value if isinstance(value, date) else None


def evaluate_capture_health(session: Session, as_of: date | None = None) -> CaptureHealthReport:
    as_of_date = as_of or datetime.now(tz=ET).date()
    expected = expected_capture_date(as_of_date)
    portfolio_expected = expected_capture_date_portfolio(as_of_date)
    # Any close bar (listed market or fund NAV) dated expected clears this
    # pipeline. Intentional v1 coarseness: "price" absent from the alert
    # means at least one close exists that day, not every venue is healthy.
    price_last = session.execute(
        select(func.max(PriceSnapshot.trade_date)).where(PriceSnapshot.session_node == "close")
    ).scalar_one()
    fx_last = _max_date(session, FxRate.rate_date)
    bench_last = _max_date(session, BenchmarkPrice.price_date)
    complete_last = session.execute(
        select(func.max(PortfolioSnapshotBatch.snapshot_date)).where(
            PortfolioSnapshotBatch.status == "complete"
        )
    ).scalar_one()
    skipped = int(
        session.execute(
            select(func.count()).where(
                PortfolioSnapshotBatch.snapshot_date == portfolio_expected,
                PortfolioSnapshotBatch.status == "skipped_deps",
            )
        ).scalar_one()
    )
    pending = int(
        session.execute(
            select(func.count()).where(
                PortfolioSnapshotBatch.snapshot_date == portfolio_expected,
                PortfolioSnapshotBatch.status == "pending",
            )
        ).scalar_one()
    )
    issues: list[str] = []
    if is_stale(price_last, expected):
        issues.append("price")
    if is_stale(fx_last, expected):
        issues.append("fx")
    if should_alert_portfolio(complete_last, portfolio_expected, skipped, pending):
        issues.append("portfolio")
    if is_stale(bench_last, expected):
        issues.append("benchmark")
    report = CaptureHealthReport(
        expected_date=as_of_date,
        issues=tuple(issues),
        skipped_deps=skipped,
        pending=pending,
        price_last=price_last,
        fx_last=fx_last,
        bench_last=bench_last,
        complete_last=complete_last,
        market_expected_date=expected,
    )
    logger.info(
        "capture_health: expected=%s market_expected=%s issues=%s skipped_deps=%d pending=%d",
        as_of_date,
        expected,
        ",".join(issues) or "none",
        skipped,
        pending,
    )
    return report


# issue #426: human-facing label + plain-English effect for each issue
# code, keyed the same as the `issues` tuple built in evaluate_capture_health.
# Order here also fixes the order issue blocks render in the email body.
_ISSUE_ORDER = ("price", "fx", "benchmark", "portfolio")
_ISSUE_LABELS: dict[str, str] = {
    "price": "Market closing prices",
    "fx": "FX rates",
    "benchmark": "Benchmark index prices",
    "portfolio": "Portfolio value snapshots",
}
# issue #426's normative template gives each non-portfolio pipeline its own
# "no X dated ... yet" phrasing rather than one generic "no success dated"
# line — literal wording, not paraphrase (per the issue's Contract
# constraints: "implement as specified, not as inspiration").
_ISSUE_LEAD: dict[str, str] = {
    "price": "no closing price dated {expected} yet.",
    "fx": "no exchange rate dated {expected} yet.",
    "benchmark": "no price dated {expected} yet.",
}
_ISSUE_EFFECTS: dict[str, str] = {
    "price": "Reports and valuations for today will be missing a fresh closing price.",
    "fx": (
        "Reports and valuations will use the last-known rate above instead of "
        "today's until a fresher one lands. A second live-quote attempt at "
        "20:00 ET falls back to a second data source per pair (issue #519) "
        "before this needs a human look."
    ),
    "benchmark": "Benchmark comparisons on the Portfolio Performance chart will lag by one day.",
    "portfolio": (
        "Affected users' Portfolio Performance chart is missing today's data "
        "point until the catch-up job (issue #373) resolves it."
    ),
}


def _format_last(d: date | None) -> str:
    return d.isoformat() if d else "never"


def _render_issue_block(code: str, report: CaptureHealthReport) -> list[str]:
    expected = report.market_day().isoformat()
    if code == "portfolio":
        # No "no X dated ... yet" lead here — the template leads straight
        # with the skipped/pending counts, unlike the other three pipelines.
        lines = [
            f"- {_ISSUE_LABELS[code]}: {report.skipped_deps} user-day(s) waiting on a "
            f"missing dependency (usually FX), {report.pending} still mid-computation. "
            f"Last fully completed date: {_format_last(report.complete_last)}."
        ]
    else:
        last_by_code = {
            "price": report.price_last,
            "fx": report.fx_last,
            "benchmark": report.bench_last,
        }
        lines = [
            f"- {_ISSUE_LABELS[code]}: {_ISSUE_LEAD[code].format(expected=expected)} "
            f"Last confirmed date: {_format_last(last_by_code[code])}."
        ]
    lines.append(f"  {_ISSUE_EFFECTS[code]}")
    lines.append("")
    return lines


def _render_alert_body(report: CaptureHealthReport, fingerprint: str) -> str:
    expected = report.expected_date.isoformat()
    lines = [
        f"Portfonia data capture check — {expected} (US Eastern Time)",
        "",
        "ISSUES FOUND:",
        "",
    ]
    for code in _ISSUE_ORDER:
        if code in report.issues:
            lines.extend(_render_issue_block(code, report))
    lines += [
        "WHAT HAPPENS NEXT:",
        "No action needed unless this repeats. Each pipeline clears itself "
        "automatically the next time it captures data dated on or after "
        f"{expected} — you will not get a repeat alert for the same "
        "date/issue combination.",
        "",
        "If the SAME pipeline is flagged on multiple consecutive trading days, "
        "that is the signal worth a human look — a single day is very likely "
        "vendor timing, not a break.",
        "",
        "---",
        "Technical detail:",
        "  probe: 21:30 ET every day, detection only (does not write or retry)",
        f"  raw issue codes: {fingerprint}",
        f"  portfolio: skipped_deps={report.skipped_deps} pending={report.pending}",
        f"  dedup key: ops-capture-health-{expected}-{fingerprint}",
    ]
    return "\n".join(lines)


def maybe_alert_capture_health(report: CaptureHealthReport) -> None:
    """One ops email for the night. Production-gated; Redis-deduped."""
    if not report.should_alert():
        return
    if get_settings().APP_ENV != "production":
        return
    fingerprint = ",".join(report.issues)
    dedup_key = f"ops-capture-health-{report.expected_date.isoformat()}-{fingerprint}"
    if already_alerted(dedup_key):
        return
    human_labels = ", ".join(_ISSUE_LABELS[code] for code in _ISSUE_ORDER if code in report.issues)
    last_by_code = {
        "price": report.price_last,
        "fx": report.fx_last,
        "benchmark": report.bench_last,
    }
    is_repeat = any(
        _is_repeat(last_by_code[code], report.market_day())
        for code in report.issues
        if code in last_by_code
    )
    severity: Literal["INFO", "WARNING", "ALERT"] = "WARNING" if is_repeat else "INFO"
    if send_ops_alert(
        subject=f"[Portfonia] Capture alert: {human_labels} not confirmed for "
        f"{report.expected_date.isoformat()}",
        body=_render_alert_body(report, fingerprint),
        idempotency_key=dedup_key,
        severity=severity,
    ):
        mark_alerted(dedup_key, _ALERT_DEDUP_TTL_SECONDS)
