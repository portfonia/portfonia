"""POST /admin/users/{user_id}/reports/{report_id}/rerun (issue #324).

Ops-token action that reruns and optionally resends one already-generated
report without re-fetching news/Tavily/macro intel: `mode="analyze"` (the
default) re-runs the body pass against a fresh read of the user's live
holdings via `regenerate_report`, `mode="render"` only re-renders the stored
body. `resend=true` (the default) clears `email_sent_at`/`provider_message_id`
on the target row BEFORE calling `regenerate_report` — send_report_email's G3
dedup guard would otherwise silently no-op on an already-sent report — then
explicitly calls `send_report_email` if the resulting status is "success".
`resend=false` behaves like the existing self-service regenerate: content
updates in place, no send attempt, email_sent_at untouched.

Design contract: GitHub issue #324, second comment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.report import Report
from app.models.user import User
from app.services.user_scope import report_language_for
from app.tests.test_admin_router import _headers

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
_RID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
_OTHER_UID = uuid.UUID("00000000-0000-0000-0000-0000000000c4")


def _user(user_id: uuid.UUID, email: str, *, email_verified_at: datetime | None = None) -> User:
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
        email_verified_at=email_verified_at,
    )


def _report(
    report_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    status: str = "success",
    email_sent_at: datetime | None = None,
    provider_message_id: str | None = None,
) -> Report:
    return Report(
        id=report_id,
        user_id=user_id,
        report_date=date(2026, 9, 2),
        report_type="incremental",
        session_node="after_close",
        status=status,
        report_md="# Report\n\nBody",
        email_sent_at=email_sent_at,
        provider_message_id=provider_message_id,
    )


def _path(user_id: uuid.UUID, report_id: uuid.UUID) -> str:
    return f"/admin/users/{user_id}/reports/{report_id}/rerun"


def test_rerun_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post(_path(_UID, _RID))
    assert resp.status_code == 401


def test_rerun_404_unknown_user(app_client: TestClient, db_session: Session) -> None:
    with patch("app.routers.admin.regenerate_report") as mock_regen:
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={})

    assert resp.status_code == 404
    assert resp.json()["detail"] == "user not found"
    mock_regen.assert_not_called()


def test_rerun_404_report_not_found(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_UID, "ops-target@example.com"))
    db_session.flush()

    with patch("app.routers.admin.regenerate_report") as mock_regen:
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={})

    assert resp.status_code == 404
    assert resp.json()["detail"] == "report not found"
    mock_regen.assert_not_called()


def test_rerun_404_report_belongs_to_different_user(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add_all(
        [
            _user(_UID, "ops-target@example.com"),
            _user(_OTHER_UID, "someone-else@example.com"),
        ]
    )
    db_session.flush()
    db_session.add(_report(_RID, _OTHER_UID))
    db_session.flush()

    with patch("app.routers.admin.regenerate_report") as mock_regen:
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={})

    assert resp.status_code == 404
    assert resp.json()["detail"] == "report not found"
    mock_regen.assert_not_called()


def test_rerun_422_invalid_mode(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_UID, "ops-target@example.com"))
    db_session.flush()
    db_session.add(_report(_RID, _UID))
    db_session.flush()

    with patch("app.routers.admin.regenerate_report") as mock_regen:
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={"mode": "refetch"})

    assert resp.status_code == 422
    mock_regen.assert_not_called()


def test_rerun_resend_true_clears_email_state_and_resends(
    app_client: TestClient, db_session: Session
) -> None:
    """The core #324 mechanism: resend=true must clear email_sent_at BEFORE
    regenerate_report runs, then explicitly call send_report_email — G3's
    dedup guard on send_report_email would otherwise silently no-op."""
    db_session.add(_user(_UID, "ops-target@example.com"))
    db_session.flush()
    original = _report(
        _RID,
        _UID,
        email_sent_at=datetime(2026, 9, 2, 22, 0, tzinfo=UTC),
        provider_message_id="old-message-id",
    )
    db_session.add(original)
    db_session.flush()

    regenerated = _report(_RID, _UID, status="success")

    def _fake_regenerate(session: Session, report_id: uuid.UUID, **kwargs: object) -> Report:
        # The row handed back to regenerate_report must already have its
        # email state cleared — the whole point of clearing before, not
        # after, this call.
        row = session.get(Report, report_id)
        assert row is not None
        assert row.email_sent_at is None
        assert row.provider_message_id is None
        return regenerated

    with (
        patch("app.routers.admin.regenerate_report", side_effect=_fake_regenerate) as mock_regen,
        patch("app.routers.admin.send_report_email", return_value=True) as mock_send,
    ):
        resp = app_client.post(
            _path(_UID, _RID), headers=_headers(), json={"mode": "analyze", "resend": True}
        )

    assert resp.status_code == 200
    mock_regen.assert_called_once()
    kwargs = mock_regen.call_args.kwargs
    assert kwargs["user_id"] == _UID
    assert kwargs["mode"] == "analyze"
    # The call site resolves output_lang via report_language_for (the
    # target user's own locale, issue #308) — pinned here rather than
    # hardcoding "zh" so a change to that resolution shows up as a real
    # assertion failure, not a silent pass (PR #326 review leftover).
    assert kwargs["output_lang"] == report_language_for(
        db_session, _UID, get_settings().OUTPUT_LANG
    )
    mock_send.assert_called_once_with(regenerated, db_session)

    body = resp.json()
    assert body["report_id"] == str(_RID)
    assert body["user_id"] == str(_UID)
    assert body["status"] == "success"
    assert body["mode"] == "analyze"


def test_rerun_resend_false_skips_email_and_leaves_state_untouched(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "ops-target@example.com"))
    db_session.flush()
    sent_at = datetime(2026, 9, 2, 22, 0, tzinfo=UTC)
    original = _report(_RID, _UID, email_sent_at=sent_at, provider_message_id="keep-me")
    db_session.add(original)
    db_session.flush()

    regenerated = _report(
        _RID, _UID, status="success", email_sent_at=sent_at, provider_message_id="keep-me"
    )

    with (
        patch("app.routers.admin.regenerate_report", return_value=regenerated) as mock_regen,
        patch("app.routers.admin.send_report_email") as mock_send,
    ):
        resp = app_client.post(
            _path(_UID, _RID), headers=_headers(), json={"mode": "render", "resend": False}
        )

    assert resp.status_code == 200
    mock_regen.assert_called_once()
    mock_send.assert_not_called()
    body = resp.json()
    assert body["mode"] == "render"
    assert body["email_sent_at"] is not None


def test_rerun_resend_true_skips_send_when_regenerate_status_not_success(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "ops-target@example.com"))
    db_session.flush()
    db_session.add(_report(_RID, _UID))
    db_session.flush()

    failed = _report(_RID, _UID, status="failed")

    with (
        patch("app.routers.admin.regenerate_report", return_value=failed),
        patch("app.routers.admin.send_report_email") as mock_send,
    ):
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={"resend": True})

    assert resp.status_code == 200
    mock_send.assert_not_called()
    assert resp.json()["status"] == "failed"


def test_rerun_translates_regenerate_value_error_to_404(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "ops-target@example.com"))
    db_session.flush()
    db_session.add(_report(_RID, _UID))
    db_session.flush()

    with patch(
        "app.routers.admin.regenerate_report",
        side_effect=ValueError(f"report {_RID} has no stored report body to regenerate from"),
    ):
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={})

    assert resp.status_code == 404


def test_rerun_translates_llm_empty_response_to_502(
    app_client: TestClient, db_session: Session
) -> None:
    from app.services.llm_errors import LLMEmptyResponseError

    db_session.add(_user(_UID, "ops-target@example.com"))
    db_session.flush()
    db_session.add(_report(_RID, _UID))
    db_session.flush()

    with patch(
        "app.routers.admin.regenerate_report",
        side_effect=LLMEmptyResponseError("empty choices"),
    ):
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={})

    assert resp.status_code == 502
    assert "empty response" in resp.json()["detail"]


def test_rerun_translates_openai_api_error_to_502(
    app_client: TestClient, db_session: Session
) -> None:
    import httpx
    import openai

    db_session.add(_user(_UID, "ops-target@example.com"))
    db_session.flush()
    db_session.add(_report(_RID, _UID))
    db_session.flush()

    err = openai.APIError(
        "invalid api key",
        httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        body=None,
    )
    with patch("app.routers.admin.regenerate_report", side_effect=err):
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={})

    assert resp.status_code == 502
    assert "APIError" in resp.json()["detail"]


# --- issue #104 requirement #6: manual resend cooldown ---


def test_rerun_resend_blocked_by_cooldown_returns_429(
    app_client: TestClient, db_session: Session
) -> None:
    """A verified recipient in cooldown must block the whole resend action
    BEFORE email_sent_at/provider_message_id are cleared — that clearing is
    itself the overwrite poll_report_delivery's delayed read needs
    protecting from (design doc's 2026-09-03 section), not just the send."""
    db_session.add(_user(_UID, "ops-target@example.com", email_verified_at=datetime.now(tz=UTC)))
    db_session.flush()
    original = _report(
        _RID,
        _UID,
        email_sent_at=datetime(2026, 9, 2, 22, 0, tzinfo=UTC),
        provider_message_id="old-message-id",
    )
    db_session.add(original)
    db_session.flush()

    with (
        patch("app.routers.admin.check_report_resend_cooldown", return_value=420) as mock_cooldown,
        patch("app.routers.admin.regenerate_report") as mock_regen,
        patch("app.routers.admin.send_report_email") as mock_send,
    ):
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={"resend": True})

    assert resp.status_code == 429
    assert "420" in resp.json()["detail"]
    mock_cooldown.assert_called_once_with("ops-target@example.com")
    mock_regen.assert_not_called()
    mock_send.assert_not_called()

    # The row must be untouched — the whole point of checking before clearing.
    row = db_session.get(Report, _RID)
    assert row is not None
    assert row.email_sent_at == datetime(2026, 9, 2, 22, 0, tzinfo=UTC)
    assert row.provider_message_id == "old-message-id"


def test_rerun_resend_claims_cooldown_for_the_resolved_recipient_then_proceeds(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_UID, "ops-target@example.com", email_verified_at=datetime.now(tz=UTC)))
    db_session.flush()
    original = _report(
        _RID,
        _UID,
        email_sent_at=datetime(2026, 9, 2, 22, 0, tzinfo=UTC),
        provider_message_id="old-message-id",
    )
    db_session.add(original)
    db_session.flush()

    regenerated = _report(_RID, _UID, status="success")

    with (
        patch("app.routers.admin.check_report_resend_cooldown", return_value=None) as mock_cooldown,
        patch("app.routers.admin.regenerate_report", return_value=regenerated) as mock_regen,
        patch("app.routers.admin.send_report_email", return_value=True) as mock_send,
    ):
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={"resend": True})

    assert resp.status_code == 200
    mock_cooldown.assert_called_once_with("ops-target@example.com")
    mock_regen.assert_called_once()
    mock_send.assert_called_once_with(regenerated, db_session)


def test_rerun_resend_skips_cooldown_check_when_no_verified_recipient(
    app_client: TestClient, db_session: Session
) -> None:
    """No verified address means send_report_email will fail-closed on its
    own — nothing will be sent either way, so there's no overwrite risk for
    the cooldown to guard against, and it must not even be consulted."""
    db_session.add(_user(_UID, "ops-target@example.com"))  # email_verified_at unset
    db_session.flush()
    db_session.add(_report(_RID, _UID))
    db_session.flush()

    regenerated = _report(_RID, _UID, status="success")

    with (
        patch("app.routers.admin.check_report_resend_cooldown") as mock_cooldown,
        patch("app.routers.admin.regenerate_report", return_value=regenerated),
        patch("app.routers.admin.send_report_email", return_value=False),
    ):
        resp = app_client.post(_path(_UID, _RID), headers=_headers(), json={"resend": True})

    assert resp.status_code == 200
    mock_cooldown.assert_not_called()
