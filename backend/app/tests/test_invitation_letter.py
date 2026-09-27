"""Issue #569 invitation letter contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import ET
from app.models.invite import Invite
from app.models.waitlist_entry import WaitlistEntry
from app.services.email_sender import send_invitation_letter
from app.services.invites import create_invite


def test_invite_email_lookup(app_client: TestClient, db_session: Session) -> None:
    bound = create_invite(db_session, created_by=uuid.uuid4(), email="bound@example.com")
    unbound = create_invite(db_session, created_by=uuid.uuid4())
    db_session.commit()
    assert app_client.get("/auth/invite-email", params={"token": bound.token}).json() == {
        "email": "bound@example.com"
    }
    assert app_client.get("/auth/invite-email", params={"token": unbound.token}).json() == {
        "email": None
    }
    assert app_client.get("/auth/invite-email", params={"token": "unknown"}).json() == {
        "email": None
    }


def test_unsubscribe_round_trip(app_client: TestClient, db_session: Session) -> None:
    from app.services.invitation_unsubscribe import create_token, verify_token

    issued = create_invite(db_session, created_by=uuid.uuid4(), email="letter@example.com")
    db_session.commit()
    token = create_token(issued.id, "zh")
    assert verify_token(token) == (issued.id, "zh")
    assert verify_token(token + "x") is None
    assert verify_token("%") is None
    assert verify_token(create_token(issued.id, "xx")) is None
    url = f"/invitation-letters/unsubscribe?token={token}"
    assert app_client.get(url).status_code == 200
    row = db_session.get(Invite, issued.id)
    assert row is not None and row.letter_unsubscribed_at is None
    assert app_client.post(url, data={"List-Unsubscribe": "One-Click"}).status_code == 200
    first = row.letter_unsubscribed_at
    assert first is not None
    assert app_client.post(url).status_code == 200
    db_session.expire_all()
    assert row.letter_unsubscribed_at == first
    assert app_client.get("/invitation-letters/unsubscribe?token=bad").status_code == 400


def test_letter_send_persists_invite_and_blocks_unsubscribed(
    app_client: TestClient, db_session: Session
) -> None:
    from app.core.config import get_settings

    headers = {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}
    with (
        patch("app.routers.admin.send_invitation_letter", return_value="resend-1") as send,
        patch("app.routers.admin.poll_invitation_letter_delivery.apply_async") as poll,
    ):
        response = app_client.post(
            "/admin/invitation-letters", json={"email": "Letter@Example.com"}, headers=headers
        )
        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "letter@example.com"
        assert body["language"] == "en"
        assert body["waitlist_entry_id"] is None
        row = db_session.get(Invite, uuid.UUID(body["invite_id"]))
        assert row is not None and row.letter_provider_message_id == "resend-1"
        send.assert_called_once()
        poll.assert_called_once_with(args=[body["invite_id"]], countdown=600)

        row.letter_unsubscribed_at = row.letter_sent_at
        db_session.commit()
        blocked = app_client.post(
            "/admin/invitation-letters", json={"email": " LETTER@example.com "}, headers=headers
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "recipient unsubscribed from invitation letters"


def test_waitlist_locale_and_send_failure(app_client: TestClient, db_session: Session) -> None:
    entry = WaitlistEntry(email="waiting@example.com", locale="zh-Hant", status="pending")
    db_session.add(entry)
    db_session.commit()
    headers = {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}
    with (
        patch("app.routers.admin.send_invitation_letter", return_value=None) as send,
        patch("app.routers.admin.poll_invitation_letter_delivery.apply_async") as poll,
    ):
        response = app_client.post(
            "/admin/invitation-letters",
            json={"email": entry.email, "language": "en"},
            headers=headers,
        )
        assert response.status_code == 502
        assert send.call_args.kwargs["locale"] == "zh"
        poll.assert_not_called()
    db_session.refresh(entry)
    assert entry.status == "invited" and entry.link_sent_at is None
    old = db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == entry.id))
    assert old is not None and old.letter_sent_at is None and old.revoked_at is None
    with (
        patch("app.routers.admin.send_invitation_letter", return_value="resend-zh"),
        patch("app.routers.admin.poll_invitation_letter_delivery.apply_async") as poll,
    ):
        response = app_client.post(
            "/admin/invitation-letters", json={"email": entry.email}, headers=headers
        )
    assert response.status_code == 201 and response.json()["language"] == "zh"
    db_session.refresh(entry)
    db_session.refresh(old)
    assert old.revoked_at is not None
    assert entry.link_sent_at is not None
    assert app_client.get(f"/admin/waitlist/{entry.id}", headers=headers).json()["stage"] == "sent"
    poll.assert_called_once()


@pytest.mark.parametrize("status,expected", [("rejected", "entry rejected"), ("pending", None)])
def test_waitlist_refusal(
    app_client: TestClient, db_session: Session, status: str, expected: str | None
) -> None:
    entry = WaitlistEntry(email="blocked@example.com", locale="en", status=status)
    db_session.add(entry)
    db_session.commit()
    if expected is None:
        issued = create_invite(
            db_session, created_by=uuid.uuid4(), email=entry.email, waitlist_entry_id=entry.id
        )
        old = db_session.get(Invite, issued.id)
        assert old is not None
        old.used_at = datetime.now(tz=ET)
        db_session.commit()
        expected = "entry already registered"
    headers = {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}
    with patch("app.routers.admin.send_invitation_letter") as send:
        response = app_client.post(
            "/admin/invitation-letters", json={"email": entry.email}, headers=headers
        )
    assert response.status_code == 409 and response.json()["detail"] == expected
    send.assert_not_called()


def test_sender_payload_and_copy() -> None:
    invite_url = "https://portfonia.com/signup?invite=abc"
    unsubscribe_url = "https://portfonia.com/api/invitation-letters/unsubscribe?token=xyz"
    with patch("app.services.email_sender.httpx.Client") as client_class:
        response = client_class.return_value.__enter__.return_value.post.return_value
        response.json.return_value = {"id": "provider-1"}
        assert (
            send_invitation_letter(
                "someone@example.com",
                invite_url,
                unsubscribe_url,
                locale="zh",
                idempotency_key="invitation-letter:abc",
            )
            == "provider-1"
        )
        args = client_class.return_value.__enter__.return_value.post.call_args
        payload = args.kwargs["json"]
        assert payload["subject"] == "Portfonia 邀请函"
        assert payload["reply_to"] == get_settings().EMAIL_REPLY_TO
        assert payload["headers"] == {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
        assert f'<a href="{invite_url}">{invite_url}</a>' in payload["html"]
        assert invite_url in payload["text"]
        assert args.kwargs["headers"]["Idempotency-Key"] == "invitation-letter:abc"
        response.json.return_value = {}
        assert (
            send_invitation_letter(
                "someone@example.com",
                invite_url,
                unsubscribe_url,
                locale="en",
                idempotency_key="invitation-letter:abc",
            )
            is None
        )


@pytest.mark.parametrize(
    "event,alert_count", [("bounced", 1), ("delivery_delayed", 0), ("delivered", 0)]
)
def test_delivery_poll_records_event(db_session: Session, event: str, alert_count: int) -> None:
    from app.tasks.invitation_letter_tasks import poll_invitation_letter_delivery

    issued = create_invite(db_session, created_by=uuid.uuid4(), email="poll@example.com")
    row = db_session.get(Invite, issued.id)
    assert row is not None
    row.letter_provider_message_id = "resend-poll"
    row.letter_sent_at = datetime.now(tz=ET)
    db_session.commit()
    with (
        patch("app.tasks.invitation_letter_tasks.httpx.Client") as client_class,
        patch("app.tasks.invitation_letter_tasks.send_ops_alert") as alert,
    ):
        response = client_class.return_value.__enter__.return_value.get.return_value
        response.status_code = 200
        response.json.return_value = {"last_event": event}
        assert poll_invitation_letter_delivery.run(str(issued.id)) == f"ok_{event}"
        assert alert.call_count == alert_count
        if alert_count:
            assert alert.call_args.kwargs["severity"] == "ALERT"
            assert alert.call_args.kwargs["idempotency_key"] == (
                f"invitation-letter-delivery:{issued.id}:{event}"
            )
    db_session.refresh(row)
    assert row.letter_delivery_event == event


def test_delivery_poll_missing_or_unauthorized_key(db_session: Session) -> None:
    from app.tasks.invitation_letter_tasks import poll_invitation_letter_delivery

    issued = create_invite(db_session, created_by=uuid.uuid4(), email="key@example.com")
    row = db_session.get(Invite, issued.id)
    assert row is not None
    row.letter_provider_message_id = "resend-key"
    db_session.commit()
    with (
        patch("app.tasks.invitation_letter_tasks.get_settings") as settings,
        patch("app.tasks.invitation_letter_tasks.alert_resend_all_access_key_issue") as alert,
    ):
        settings.return_value.RESEND_ALL_ACCESS_API_KEY = None
        assert poll_invitation_letter_delivery.run(str(issued.id)) == "skipped_no_key"
        alert.assert_called_once_with("missing")
        alert.reset_mock()
        settings.return_value.RESEND_ALL_ACCESS_API_KEY = SecretStr("test-key")
        with patch("app.tasks.invitation_letter_tasks.httpx.Client") as client_class:
            response = client_class.return_value.__enter__.return_value.get.return_value
            response.status_code = 401
            assert poll_invitation_letter_delivery.run(str(issued.id)) == "skipped_unauthorized"
        alert.assert_called_once_with("unauthorized")
