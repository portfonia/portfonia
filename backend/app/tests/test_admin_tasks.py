"""Unit test for the admin ops-alert Celery task (issue #128 Ring 1 stage B,
checkpoint B2, PR #177 review round 3)."""

from __future__ import annotations

from unittest.mock import patch

from app.tasks.admin_tasks import send_admin_alert_task


def test_send_admin_alert_task_calls_send_ops_alert() -> None:
    with patch("app.tasks.admin_tasks.send_ops_alert") as mock_send:
        send_admin_alert_task("subject", "body")
    mock_send.assert_called_once_with("subject", "body")
