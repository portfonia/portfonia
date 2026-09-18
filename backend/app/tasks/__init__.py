"""Celery application and Beat schedule (Stage H + ADR-002 capture layer)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]
from celery.signals import before_task_publish  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.core.timezones import BERLIN, CST, ET, HKT, JST, KST, LONDON
from app.services.markets import CAPTURE_MARKET_ORDER

_settings = get_settings()

celery_app = Celery(
    "portfonia",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=[
        "app.tasks.report_tasks",
        "app.tasks.capture_tasks",
        "app.tasks.holdings_tasks",
        "app.tasks.backup_tasks",
        "app.tasks.cache_tasks",
        "app.tasks.admin_tasks",
        "app.tasks.email_verification_tasks",
        "app.tasks.report_delivery_tasks",
        "app.tasks.notification_tasks",
        "app.tasks.operational_events_tasks",
        "app.tasks.vigil_tasks",
    ],
)

# Publication-time telemetry (issue #446, Design §3): restricted to the two
# report task names only — this hook fires for EVERY task published on this
# Celery app, and instrumenting the rest is explicitly out of scope
# (Contract constraints #6). `before_task_publish` runs in the PUBLISHING
# process (the Beat/API/admin process calling `.delay()`/`.apply_async()`),
# not the worker — so this cannot use app.core.operational_events' run/span
# context (no run exists yet) and writes a standalone `emit_dispatch` record
# instead, correlated to the eventual worker-side run only via the
# `dispatch_id` header propagated below.
_TELEMETRY_TASK_NAMES = frozenset(
    {
        "app.tasks.report_tasks.generate_incremental_report",
        "app.tasks.report_tasks.generate_report_job",
    }
)


@before_task_publish.connect  # type: ignore[untyped-decorator]
def _emit_report_task_dispatch(
    sender: str | None = None,
    headers: dict[str, Any] | None = None,
    **_kwargs: Any,
) -> None:
    if sender not in _TELEMETRY_TASK_NAMES or headers is None:
        return
    from app.core.operational_events import emit_dispatch

    dispatch_id = uuid.uuid4()
    # Propagated to the worker via Celery's own task headers (picked up by
    # the task itself, e.g. `self.request.dispatch_id` — Celery merges
    # arbitrary header keys onto `self.request`). A retry publication
    # (`self.retry(...)`) goes through this same signal again and gets a
    # NEW dispatch_id; a broker redelivery of the SAME message does not
    # re-publish, so it keeps the original headers unchanged (Design §1).
    headers["dispatch_id"] = str(dispatch_id)
    headers["published_at"] = datetime.now(UTC).isoformat()
    emit_dispatch(f"{sender}.dispatch", dispatch_id=dispatch_id, task_id=headers.get("id"))


# Market session nodes (ADR-002). Each (market, tz, [(node, hour, minute)]).
# No call-auction, no after-hours for HK/CN.
# Close anchors (issue #311): UK 16:30 London, Europe 17:30 Berlin,
# Japan 15:00 Tokyo, Korea 15:30 Seoul. Open+close only — same shape as
# HK/CN (no after-hours). Order matches CAPTURE_MARKET_ORDER.
_MARKET_NODES: tuple[tuple[str, Any, tuple[tuple[str, int, int], ...]], ...] = (
    ("US", ET, (("pre_open", 9, 0), ("open", 9, 30), ("close", 16, 0), ("after_close", 20, 0))),
    ("HK", HKT, (("open", 9, 30), ("close", 16, 0))),
    ("A-Share", CST, (("open", 9, 30), ("close", 15, 0))),
    ("UK", LONDON, (("open", 8, 0), ("close", 16, 30))),
    ("Europe", BERLIN, (("open", 9, 0), ("close", 17, 30))),
    ("Japan", JST, (("open", 9, 0), ("close", 15, 0))),
    ("Korea", KST, (("open", 9, 0), ("close", 15, 30))),
)
assert tuple(m for m, _, _ in _MARKET_NODES) == CAPTURE_MARKET_ORDER


class _NowIn:
    """Picklable nowfun: returns the current time in a fixed zone.

    Must be a top-level callable, not a lambda/closure — Beat's
    PersistentScheduler shelves (pickles) the schedule, and a local lambda is not
    picklable (it crashes beat at startup).
    """

    def __init__(self, tz: Any) -> None:
        self._tz = tz

    def __call__(self) -> datetime:
        return datetime.now(self._tz)


def _node_cron(tz: Any, hour: int, minute: int) -> crontab:
    """A crontab evaluated in *tz* via nowfun.

    This is how one Beat instance schedules across DST regimes: US uses ET
    (DST-aware), HK/CN use their own zones (no DST → effectively fixed UTC+8).
    """
    return crontab(hour=hour, minute=minute, nowfun=_NowIn(tz))


# Report cadences: (beat entry name, report_type, session_node, crontab
# kwargs, cadence). `cadence` matches a users.report_cadence value and drives
# app.services.user_scope.active_user_ids' fan-out query — adding a cadence
# is a table row, not a new task function, since generate_incremental_report
# takes report_type/session_node/cadence as arguments rather than hardcoding
# them. Ring 1 may extend this with monthly/daily_brief; those aren't real
# yet (no Beat row, no way to set them on a user) so they aren't
# pre-enumerated here or in users.VALID_REPORT_CADENCES — see the Ring 1-B
# Cadence design doc, decision point 3.
#
# `weekly` (issue #191) fires Saturday 19:00 ET rather than at a real market
# close. Portfolio-value snapshot capture is 20:30 ET every day (issue #487),
# so a Saturday weekly report still reads Friday's snapshot; later same-day
# capture does not feed this Beat row. `session_node="weekend_snapshot"`
# names that explicitly rather than reusing "after_close", which would imply
# a close event that didn't happen. The macro/news layer (ticker_intel.py /
# cross_name_intel.py) is a live, generation-time search keyed by trade_date,
# not tied to these capture nodes — so a Saturday report's holdings data is
# a stable weekday-old snapshot, but its macro content can still reflect
# the weekend.
_REPORT_CADENCES: tuple[tuple[str, str, str, dict[str, Any], str], ...] = (
    (
        "report-incremental-mwf",
        "incremental",
        "after_close",
        {"hour": 17, "minute": 0, "day_of_week": "mon,wed,fri"},
        "mwf",
    ),
    (
        "report-incremental-weekly",
        "incremental",
        "weekend_snapshot",
        {"hour": 19, "minute": 0, "day_of_week": "sat"},
        "weekly",
    ),
)


def next_occurrence_for_cadence(cadence: str, now: datetime) -> datetime:
    """Next ET fire time for *cadence*, per `_REPORT_CADENCES` (issue #202).

    Reads the same `cron_kwargs` Beat schedules from, via `crontab.
    remaining_estimate`, rather than a second hand-rolled weekday/hour
    calculation that could drift from the real schedule.

    `remaining_estimate` measures "remaining" from its own `nowfun()`, not
    from the `last_run_at` argument (that argument only anchors which past
    occurrence to search forward from) — so `nowfun` must be pinned to
    *now_et* or the result silently drifts to whatever the real wall clock
    is when this happens to run, exactly the `_NowIn` problem `_node_cron`
    already solves for Beat's own schedule.
    """
    for _, _, _, cron_kwargs, row_cadence in _REPORT_CADENCES:
        if row_cadence == cadence:
            now_et = now.astimezone(ET)
            cron = crontab(**cron_kwargs, nowfun=lambda pinned=now_et: pinned)
            delta: timedelta = cron.remaining_estimate(now_et)
            return now_et + delta
    raise ValueError(f"unknown report cadence: {cadence!r}")


def _build_report_schedule() -> dict[str, dict[str, Any]]:
    sched: dict[str, dict[str, Any]] = {}
    for name, report_type, session_node, cron_kwargs, cadence in _REPORT_CADENCES:
        sched[name] = {
            "task": "app.tasks.report_tasks.generate_incremental_report",
            "schedule": crontab(**cron_kwargs),
            "kwargs": {
                "report_type": report_type,
                "session_node": session_node,
                "cadence": cadence,
                # Beat's PersistentScheduler fires a missed crontab tick as soon
                # as it comes back up (e.g. after a machine reboot took the
                # scheduler down for days) instead of skipping it — the task
                # verifies its own invocation is actually close to this
                # intended fire time so a stale catch-up run doesn't silently
                # generate + email an unwanted report. See issue #71.
                "trigger_hour": cron_kwargs["hour"],
                "trigger_minute": cron_kwargs.get("minute", 0),
            },
        }
    return sched


def _build_capture_schedule() -> dict[str, dict[str, Any]]:
    sched: dict[str, dict[str, Any]] = {}
    for market, tz, nodes in _MARKET_NODES:
        for node, hour, minute in nodes:
            sched[f"capture-prices-{market}-{node}"] = {
                "task": "app.tasks.capture_tasks.capture_prices_task",
                "schedule": _node_cron(tz, hour, minute),
                "args": (market, node),
            }
            # Piggyback a news fetch here — news is global; dedup makes the overlap free.
            sched[f"capture-news-{market}-{node}"] = {
                "task": "app.tasks.capture_tasks.capture_news_task",
                "schedule": _node_cron(tz, hour, minute),
            }
    return sched


_beat_schedule: dict[str, dict[str, Any]] = {
    # Forward calendar (#1): refresh the next ~2 weeks of US macro + earnings dates
    # once a day, before US pre-open. Catch-up is in the task (idempotent upsert).
    "capture-forward-events-daily": {
        "task": "app.tasks.capture_tasks.capture_forward_events_task",
        "schedule": crontab(hour=8, minute=0, day_of_week="mon-fri"),
    },
    # FX rates (R-4): 19:30 ET every calendar day (issue #509; was 17:15 ET,
    # before that 16:05 like the equities close nodes, issue #258). 16:05
    # and then 17:15 both consistently captured the *previous* day's daily
    # bar (confirmed against production fx_rates rows on both timings, off
    # by exactly one day every time) — a live probe on 2026-09-17 against
    # the actual production fetch path found the FX daily bar's own
    # rollover lands around 19:00 ET, not the ~17:00 ET earlier timings
    # assumed. 19:30 ET leaves a buffer past that, with a second same-day
    # attempt at 20:00 ET (capture-fx-catchup-daily) before the 20:30 ET
    # portfolio snapshot locks in the day's data quality. A genuine
    # non-trading day is an idempotent source-dated upsert of the last bar,
    # not a new row dated "today".
    "capture-fx-daily": {
        "task": "app.tasks.capture_tasks.capture_fx_task",
        "schedule": crontab(hour=19, minute=30),
    },
    # Fund NAV (Tiantian Fund): settled NAV for fund_code holdings is published by
    # the fund manager after A-share close (usually same evening). 20:00 CST
    # every calendar day (issue #487); idempotent upsert in price_snapshots
    # keyed by the source NAV date, not fetch-time.
    "capture-fund-navs-daily": {
        "task": "app.tasks.capture_tasks.capture_fund_navs_task",
        "schedule": crontab(hour=20, minute=0, nowfun=_NowIn(CST)),
    },
    # Stuck-pending UploadJob backstop (issue #85): a plain interval, not a
    # crontab — this is a fast, always-on sweep, not a market-session-timed
    # one. 30s keeps the sweeper's own detection lag small relative to the
    # 60s stale threshold (holdings_tasks._SWEEP_STALE_AFTER_SECONDS) it's
    # backstopping.
    "sweep-stale-upload-jobs": {
        "task": "app.tasks.holdings_tasks.sweep_stale_upload_jobs",
        "schedule": 30.0,
    },
    # Vigil outbox dispatch sweep (issue #456, P3.1): bounded to 5 due rows
    # per invocation (dispatch.MAX_ROWS_PER_SWEEP) — a plain interval, not a
    # market-session crontab, since a pending mail intent can become due at
    # any time of day. 30s matches the stale-upload-job sweep's cadence;
    # VIGIL_MODE=off (default) makes every invocation a fast no-op query.
    "sweep-vigil-outbox": {
        "task": "app.tasks.vigil_tasks.dispatch_vigil_outbox_task",
        "schedule": 30.0,
    },
    # Vigil delivery-evidence poll (issue #457, P3.2): missing provider
    # facts only, at 5/15/30 minutes after first_attempt_at, bounded to 5
    # GETs per invocation. Same existing worker/beat as the outbox sweep.
    "poll-vigil-delivery": {
        "task": "app.tasks.vigil_tasks.poll_vigil_delivery_task",
        "schedule": 30.0,
    },
    # Vigil three-round confirmation scan (issue #459, P3.3): at most one
    # level step per invocation, 60s on the existing beat/worker/queue.
    "scan-vigil-cycles": {
        "task": "app.tasks.vigil_tasks.scan_vigil_cycles_task",
        "schedule": 60.0,
    },
    # Upload-job retention sweep (issue #264): daily cleanup of upload_jobs
    # rows (holdings preview JSONB + terminal shell rows) older than 30
    # days. 04:30 ET — every day, staggered from the 03:00 ET backup and
    # the 04:00 ET shared-intel-cache sweep.
    "cleanup-upload-jobs-daily": {
        "task": "app.tasks.holdings_tasks.cleanup_upload_jobs",
        "schedule": crontab(hour=4, minute=30),
    },
    # Daily Postgres -> OCI Object Storage backup (issue #106). Runs at
    # 03:00 ET, off-peak relative to every other daily cadence (forward
    # events 08:00 ET, FX 16:05 ET, fund NAV 20:00 CST). Every day, not just
    # trading days — a weekend DB state (e.g. an in-progress holdings
    # confirm) is still worth a restore point. No-ops locally
    # (BACKUP_OCI_NAMESPACE unset by default — see Settings).
    "backup-database-daily": {
        "task": "app.tasks.backup_tasks.backup_database_task",
        "schedule": crontab(hour=3, minute=0),
    },
    # L1 shared-intel cache retention (issue #128 A2): ticker_intel/
    # search_cache grow one row per (identifier|query, trade_date) per day
    # under multi-user fan-out — see app/tasks/cache_tasks.py module
    # docstring. Off-peak, distinct from the 03:00 ET backup and the other
    # daily cadences (forward events 08:00 ET, FX 16:05 ET, fund NAV 20:00
    # CST). Every day, not just trading days, matching backup-database-daily's
    # rationale.
    "sweep-stale-shared-intel-cache-daily": {
        "task": "app.tasks.cache_tasks.sweep_stale_shared_intel_cache",
        "schedule": crontab(hour=4, minute=0),
    },
    # Portfolio Performance (issue #360 Phase 1). 20:30 ET every calendar
    # day (issue #487) — after every market's close node (latest is US
    # after_close at 20:00 ET) and after the 17:15 ET FX fetch, so a user's
    # day almost always resolves its FX dependency on the first try
    # (capture_portfolio_value_snapshot's own per-user skipped_deps check
    # covers the rare case it doesn't). A Saturday/Sunday run writes a
    # complete batch with carried-forward marks disclosed as approx_carried.
    "capture-portfolio-value-snapshot-daily": {
        "task": "app.tasks.capture_tasks.capture_portfolio_value_snapshot_task",
        "schedule": crontab(hour=20, minute=30),
    },
    # Same cadence as the snapshot task above — benchmark closes (sp500/
    # dow30/nasdaq/csi300, D9 + #383) are independent of holdings/FX and
    # could run earlier, but sharing one fixed time keeps the schedule easy
    # to reason about; both tasks are idempotent source-dated upserts
    # (issue #487: every calendar day; no new bar → no new dated row).
    "capture-benchmark-index-prices-daily": {
        "task": "app.tasks.capture_tasks.capture_benchmark_index_prices_task",
        "schedule": crontab(hour=20, minute=30),
    },
    # Issue #372 slice B: lag/skipped_deps probe after the 20:30 ET window.
    # Every calendar day (issue #487) so a failed weekend portfolio capture
    # is detected that night, not deferred to Monday.
    "check-capture-health-daily": {
        "task": "app.tasks.capture_tasks.check_capture_health_task",
        "schedule": crontab(hour=21, minute=30),
    },
    # FX catch-up (issue #426, retimed same-day by issue #509): a second,
    # same-day attempt 30 minutes before the 20:30 ET portfolio snapshot —
    # retry + Twelve Data fallback for any pair the 19:30 ET fetch above
    # still missed. The original design ran this at 00:05 ET the *next*
    # calendar day (tue-sat, targeting the previous ET weekday): that could
    # only ever repair `fx_rates` for future reads, never that day's own
    # snapshot, which had already locked in `approx_carried` seven-plus
    # hours earlier at 20:30 ET. Every calendar day now, targeting *today*
    # (via `expected_capture_date`, which still rolls a weekend run back to
    # the last real trading day — there is no Saturday-dated FX bar).
    "capture-fx-catchup-daily": {
        "task": "app.tasks.capture_tasks.capture_fx_catchup_task",
        "schedule": crontab(hour=20, minute=0),
    },
    # operational_events retention sweep (issue #446, Design §4): 05:00 UTC
    # specifically, not ET like this schedule's other daily entries — the
    # confirmed 90-day retention policy is a UTC-window policy (occurred_at/
    # recorded_at are stored in UTC), not tied to a US market session.
    "cleanup-operational-events-daily": {
        "task": "app.tasks.operational_events_tasks.cleanup_operational_events",
        "schedule": crontab(hour=5, minute=0, nowfun=_NowIn(UTC)),
    },
}
_beat_schedule.update(_build_report_schedule())
_beat_schedule.update(_build_capture_schedule())

celery_app.conf.update(
    # App default zone (ET) governs entries without their own nowfun.
    timezone="America/New_York",
    enable_utc=True,
    # Serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Heartbeat / reliability
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    beat_schedule=_beat_schedule,
)
