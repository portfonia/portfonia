"""Celery application and Beat schedule (Stage H)."""

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]

from app.core.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "portfonia",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["app.tasks.report_tasks"],
)

celery_app.conf.update(
    # Use America/New_York so crontab expressions follow ET — DST-safe.
    timezone="America/New_York",
    enable_utc=True,
    # Serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Heartbeat / reliability
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    # Beat schedule
    beat_schedule={
        "weekly-report-friday": {
            "task": "app.tasks.report_tasks.generate_weekly_report",
            # Every Friday at 16:30 ET (after US equity market close).
            "schedule": crontab(hour=16, minute=30, day_of_week="friday"),
        },
    },
)
