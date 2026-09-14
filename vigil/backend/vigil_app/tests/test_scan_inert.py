"""Scan is registered but must not write heartbeat or arm anything."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from vigil_app.tasks import celery_app
from vigil_app.tasks.scan import scan_due_vaults


def test_scan_task_is_registered_on_vigil_celery() -> None:
    assert "vigil_app.tasks.scan.scan_due_vaults" in celery_app.tasks
    assert "scan-due-vaults" in celery_app.conf.beat_schedule


def test_scan_does_not_stamp_heartbeat_or_create_business_rows(db_session: Session) -> None:
    result = scan_due_vaults()
    assert result["status"] == "inert"
    last_scan = db_session.execute(
        text("SELECT last_scan_completed_at FROM runtime_heartbeat WHERE id = 1")
    ).scalar_one()
    assert last_scan is None
    vault_count = db_session.execute(text("SELECT count(*) FROM vaults")).scalar_one()
    assert vault_count == 0
