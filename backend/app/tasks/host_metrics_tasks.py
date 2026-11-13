"""Five-minute host sampler (#886)."""

from app.core.database import SessionLocal
from app.services.host_metrics import sample_and_alert
from app.tasks import celery_app


@celery_app.task(name="app.tasks.host_metrics_tasks.sample_host")  # type: ignore[untyped-decorator]
def sample_host() -> dict[str, str]:
    with SessionLocal() as session:
        row = sample_and_alert(session)
        return {"id": str(row.id)}
