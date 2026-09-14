"""Independent Celery app. Must not import Portfonia task modules."""

from celery import Celery  # type: ignore[import-untyped]

from vigil_app.core.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "vigil",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["vigil_app.tasks.scan"],
)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_default_queue=f"{_settings.CELERY_QUEUE_PREFIX}.default",
    broker_transport_options={"global_keyprefix": f"{_settings.REDIS_KEY_PREFIX}:"},
    beat_schedule={
        "scan-due-vaults": {
            "task": "vigil_app.tasks.scan.scan_due_vaults",
            "schedule": 60.0,
        }
    },
)
