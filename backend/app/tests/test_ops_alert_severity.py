"""Issue #484: every remaining send_ops_alert site states severity explicitly.

Sites that already have a trigger test get a severity assertion there.
This file covers the wrapper signatures, the admin passthrough, and the
operational_events sweep (no prior exhaustion-alert test).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services import fx_fetcher, price_capture
from app.tasks.admin_tasks import send_admin_alert_task


def test_send_fx_alert_requires_severity() -> None:
    with pytest.raises(TypeError, match="severity"):
        fx_fetcher._send_fx_alert("subject", "body", "dedup-key")  # type: ignore[call-arg]


def test_send_nav_alert_requires_severity() -> None:
    with pytest.raises(TypeError, match="severity"):
        price_capture._send_nav_alert("subject", "body", "dedup-key")  # type: ignore[call-arg]


def test_send_admin_alert_task_defaults_to_alert() -> None:
    with patch("app.tasks.admin_tasks.send_ops_alert") as mock_send:
        send_admin_alert_task("subject", "body")
    mock_send.assert_called_once_with("subject", "body", severity="ALERT")


def test_send_admin_alert_task_forwards_explicit_severity() -> None:
    with patch("app.tasks.admin_tasks.send_ops_alert") as mock_send:
        send_admin_alert_task("subject", "body", severity="WARNING")
    mock_send.assert_called_once_with("subject", "body", severity="WARNING")


@patch("app.tasks.operational_events_tasks.send_ops_alert")
@patch(
    "app.tasks.operational_events_tasks.cleanup_expired_events",
    side_effect=RuntimeError("DB down"),
)
@patch("app.core.database.SessionLocal")
def test_cleanup_operational_events_alerts_warning_on_exhaustion(
    mock_session_cls: MagicMock,
    _cleanup: MagicMock,
    mock_alert: MagicMock,
) -> None:
    from app.tasks.operational_events_tasks import cleanup_operational_events

    mock_session_cls.return_value = MagicMock()
    with (
        patch.object(cleanup_operational_events, "max_retries", 0),
        pytest.raises(RuntimeError),
    ):
        cleanup_operational_events.run()

    mock_alert.assert_called_once()
    assert mock_alert.call_args.kwargs["severity"] == "WARNING"
    assert "operational_events retention sweep FAILED" in mock_alert.call_args.kwargs["subject"]
