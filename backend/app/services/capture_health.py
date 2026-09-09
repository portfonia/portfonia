"""Capture-health probe (issue #372 slice B).

Alert rule (the one rule): Mon-Fri 21:30 ET probe; expected date = that ET
weekday. A pipeline is stale if it has no success evidence dated on that
day. 36h wall-clock is not used — a Monday probe vs Friday evidence is 72h
and would false-positive every weekend.

Hard-fail (retries exhausted) stays on capture_tasks._capture_failed.
This module only notices lag and non-complete portfolio batches.

Does not write snapshots, invent days, or replay an outbox (#373).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

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


def is_stale(last_success: date | None, expected: date) -> bool:
    return last_success is None or last_success < expected


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

    def should_alert(self) -> bool:
        return bool(self.issues)

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_date": self.expected_date.isoformat(),
            "issues": list(self.issues),
            "skipped_deps": self.skipped_deps,
            "pending": self.pending,
        }


def _max_date(session: Session, column: object) -> date | None:
    value = session.execute(select(func.max(column))).scalar_one()
    return value if isinstance(value, date) else None


def evaluate_capture_health(session: Session, as_of: date | None = None) -> CaptureHealthReport:
    expected = expected_capture_date(as_of or datetime.now(tz=ET).date())
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
                PortfolioSnapshotBatch.snapshot_date == expected,
                PortfolioSnapshotBatch.status == "skipped_deps",
            )
        ).scalar_one()
    )
    pending = int(
        session.execute(
            select(func.count()).where(
                PortfolioSnapshotBatch.snapshot_date == expected,
                PortfolioSnapshotBatch.status == "pending",
            )
        ).scalar_one()
    )
    issues: list[str] = []
    if is_stale(price_last, expected):
        issues.append("price")
    if is_stale(fx_last, expected):
        issues.append("fx")
    if should_alert_portfolio(complete_last, expected, skipped, pending):
        issues.append("portfolio")
    if is_stale(bench_last, expected):
        issues.append("benchmark")
    report = CaptureHealthReport(
        expected_date=expected,
        issues=tuple(issues),
        skipped_deps=skipped,
        pending=pending,
    )
    logger.info(
        "capture_health: expected=%s issues=%s skipped_deps=%d pending=%d",
        expected,
        ",".join(issues) or "none",
        skipped,
        pending,
    )
    return report


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
    body = (
        f"expected capture date: {report.expected_date.isoformat()} (ET weekday)\n"
        f"stale or non-complete pipelines: {fingerprint}\n"
        f"portfolio skipped_deps={report.skipped_deps} pending={report.pending}\n"
        "\nRule: no success evidence dated on the expected ET weekday "
        "(probe 21:30 ET Mon-Fri). Not a 36h clock — weekend gap would false-fire.\n"
        "Hard-fail retries still go through capture_tasks._capture_failed.\n"
        "Visibility only (#372). No snapshot write, no outbox replay (#373).\n"
        "Silence: APP_ENV != production, or wait for a successful capture "
        "on a later weekday (new dedup key)."
    )
    if send_ops_alert(
        subject=f"[Portfonia] capture health — {fingerprint}",
        body=body,
        idempotency_key=dedup_key,
    ):
        mark_alerted(dedup_key, _ALERT_DEDUP_TTL_SECONDS)
