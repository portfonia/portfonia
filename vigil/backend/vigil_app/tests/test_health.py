"""P1.1-A03: ready HTTP does not stamp last_scan_completed_at."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from vigil_app.main import app


def test_ready_leaves_last_scan_null_and_returns_no_secrets(
    db_session: Session,
) -> None:
    before = db_session.execute(
        text("SELECT last_scan_completed_at FROM runtime_heartbeat WHERE id = 1")
    ).scalar_one()
    assert before is None

    client = TestClient(app)
    response = client.get("/health/ready")
    assert response.status_code in {200, 503}
    body = response.json()
    assert body["status"] in {"ready", "degraded"}
    blob = response.text.lower()
    assert "kek" not in blob
    assert "password" not in blob
    assert "token" not in blob
    assert "secret" not in blob

    after = db_session.execute(
        text("SELECT last_scan_completed_at FROM runtime_heartbeat WHERE id = 1")
    ).scalar_one()
    assert after is None


def test_ready_does_not_create_outbox_or_email_side_effects(db_session: Session) -> None:
    tables = {
        row[0]
        for row in db_session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
    }
    assert "dispatch_outbox" not in tables
    client = TestClient(app)
    client.get("/health/ready")
    remaining = {
        row[0]
        for row in db_session.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
    }
    assert remaining == tables
