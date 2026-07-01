"""Celery application and Beat schedule (Stage H + ADR-002 capture layer)."""

from datetime import datetime
from typing import Any

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.core.timezones import CST, ET, HKT

_settings = get_settings()

celery_app = Celery(
    "portfonia",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["app.tasks.report_tasks", "app.tasks.capture_tasks"],
)

# Market session nodes (ADR-002). Each (market, tz, [(node, hour, minute)]).
# No call-auction, no after-hours for HK/CN.
_MARKET_NODES: tuple[tuple[str, Any, tuple[tuple[str, int, int], ...]], ...] = (
    ("US", ET, (("pre_open", 9, 0), ("open", 9, 30), ("close", 16, 0), ("after_close", 20, 0))),
    ("HK", HKT, (("open", 9, 30), ("close", 16, 0))),
    ("A-Share", CST, (("open", 9, 30), ("close", 15, 0))),
)


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


# Report cadences: (beat entry name, report_type, session_node, crontab kwargs).
# Ring 1 will extend this with monthly/weekly/daily_brief cadences (see
# Hermes/Portfonia/Docs/多周期报告机制改造计划.md) — adding one is a table row, not
# a new task function, since generate_incremental_report takes report_type/
# session_node as arguments rather than hardcoding them.
_REPORT_CADENCES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        "report-incremental-mwf",
        "incremental",
        "after_close",
        {"hour": 17, "minute": 0, "day_of_week": "mon,wed,fri"},
    ),
)


def _build_report_schedule() -> dict[str, dict[str, Any]]:
    sched: dict[str, dict[str, Any]] = {}
    for name, report_type, session_node, cron_kwargs in _REPORT_CADENCES:
        sched[name] = {
            "task": "app.tasks.report_tasks.generate_incremental_report",
            "schedule": crontab(**cron_kwargs),
            "kwargs": {
                "report_type": report_type,
                "session_node": session_node,
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
            # "顺带抓一次新闻" — news is global; dedup makes the overlap free.
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
    # FX rates (R-4): pull once per US trading day, just after the regular close,
    # so report valuations don't drift on stale rates. Idempotent upsert.
    "capture-fx-daily": {
        "task": "app.tasks.capture_tasks.capture_fx_task",
        "schedule": crontab(hour=16, minute=5, day_of_week="mon-fri"),
    },
    # Fund NAV (天天基金): settled NAV for fund_code holdings is published by
    # the fund manager after A-share close (usually same evening). 20:00 CST
    # gives enough buffer; idempotent upsert in price_snapshots.
    "capture-fund-navs-daily": {
        "task": "app.tasks.capture_tasks.capture_fund_navs_task",
        "schedule": crontab(hour=20, minute=0, day_of_week="mon-fri", nowfun=_NowIn(CST)),
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
