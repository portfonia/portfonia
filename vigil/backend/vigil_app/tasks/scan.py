"""Periodic scan registration. Business transitions stay inert until P3.3."""

from vigil_app.core.celery import celery_app


@celery_app.task(name="vigil_app.tasks.scan.scan_due_vaults")  # type: ignore[untyped-decorator]
def scan_due_vaults() -> dict[str, str]:
    """Registered so workers start; does not stamp heartbeat or mutate vaults."""
    return {"status": "inert"}
