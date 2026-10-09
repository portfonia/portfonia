"""Jade's nightly shared history fill; no report or notification side effects."""

from dataclasses import asdict

from app.core.database import SessionLocal
from app.core.timezones import today_et
from app.services.jade_price_history import refresh_jade_price_history
from app.tasks import celery_app


@celery_app.task(name="app.tasks.jade_tasks.refresh_jade_price_history_task")  # type: ignore[untyped-decorator]
def refresh_jade_price_history_task() -> dict[str, int]:
    with SessionLocal() as session:
        return asdict(refresh_jade_price_history(session, today_et()))
