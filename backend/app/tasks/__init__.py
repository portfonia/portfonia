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


def _node_cron(tz: Any, hour: int, minute: int) -> crontab:
    """A crontab evaluated in *tz* via nowfun.

    This is how one Beat instance schedules across DST regimes: US uses ET
    (DST-aware), HK/CN use their own zones (no DST → effectively fixed UTC+8).
    `tz` is a per-call parameter, so the closure binds the correct zone.
    """
    return crontab(hour=hour, minute=minute, nowfun=lambda: datetime.now(tz))


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
    # Report cadence: incremental report Mon/Wed/Fri 16:30 ET (after US regular
    # close). app timezone is ET, so no per-entry nowfun is needed here.
    "report-incremental-mwf": {
        "task": "app.tasks.report_tasks.generate_incremental_report",
        "schedule": crontab(hour=16, minute=30, day_of_week="mon,wed,fri"),
    },
}
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
