"""Issue #569 invitation letter contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_engine
from app.core.timezones import ET
from app.models.invite import Invite
from app.models.waitlist_entry import WaitlistEntry
from app.services.email_sender import send_invitation_letter
from app.services.invitation_unsubscribe import verify_token
from app.services.invites import create_invite
from app.tests.conftest import seed_user


def _ops_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}


def _resend_success(client_class: MagicMock, provider_id: str = "resend-test") -> None:
    response = client_class.return_value.__enter__.return_value.post.return_value
    response.json.return_value = {"id": provider_id}
    response.status_code = 200


@pytest.mark.parametrize("intervention", ["remint", "rejected", "pending"])
def test_send_completion_does_not_overwrite_concurrent_ops_change(
    session_test_db: None, intervention: str
) -> None:
    """The second request commits on a separate connection while the sender is in flight."""
    from app.routers.admin import (
        InvitationLetterBody,
        WaitlistInviteBody,
        WaitlistStatusBody,
        mint_waitlist_invite,
        send_invitation_letter_endpoint,
        set_waitlist_status,
    )

    email = f"race-{intervention}@example.com"
    engine = get_engine()
    with Session(engine) as seed:
        entry = WaitlistEntry(email=email, locale="en", status="pending")
        seed.add(entry)
        seed.commit()
        entry_id = entry.id
    try:

        def during_send(*_args: object, **_kwargs: object) -> str:
            with Session(engine) as second:
                if intervention == "remint":
                    mint_waitlist_invite(entry_id, WaitlistInviteBody(), session=second, _=None)
                else:
                    set_waitlist_status(
                        entry_id,
                        WaitlistStatusBody(
                            status="rejected" if intervention == "rejected" else "pending"
                        ),
                        session=second,
                    )
            return "resend-race"

        with (
            Session(engine) as first,
            patch("app.routers.admin.send_invitation_letter", side_effect=during_send),
            patch("app.routers.admin.poll_invitation_letter_delivery.apply_async"),
        ):
            response = send_invitation_letter_endpoint(
                InvitationLetterBody(email=email), session=first, _=None
            )
            letter_id = response.invite_id
        with Session(engine) as check:
            current = check.get(WaitlistEntry, entry_id)
            sent_invite = check.get(Invite, letter_id)
            assert current is not None and current.link_sent_at is None
            assert sent_invite is not None
            assert sent_invite.letter_provider_message_id == "resend-race"
            assert sent_invite.letter_sent_at is not None
            if intervention == "remint":
                assert current.status == "invited"
                assert sent_invite.revoked_at is not None
            else:
                assert current.status == intervention
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(Invite).where(Invite.waitlist_entry_id == entry_id))
            cleanup.execute(delete(WaitlistEntry).where(WaitlistEntry.id == entry_id))
            cleanup.commit()


def test_completion_lock_blocks_status_change_until_sent_commit(session_test_db: None) -> None:
    """A status edit started after the re-check must wait for the completion commit."""
    from app.routers.admin import (
        InvitationLetterBody,
        WaitlistStatusBody,
        send_invitation_letter_endpoint,
        set_waitlist_status,
    )

    engine = get_engine()
    with Session(engine) as seed:
        entry = WaitlistEntry(email="race-after-recheck@example.com", locale="en", status="pending")
        seed.add(entry)
        seed.commit()
        entry_id = entry.id

    started = threading.Event()
    finished = threading.Event()
    errors: list[Exception] = []

    def edit_status() -> None:
        started.set()
        try:
            with Session(engine) as second:
                set_waitlist_status(entry_id, WaitlistStatusBody(status="rejected"), session=second)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker: threading.Thread | None = None
    try:
        with Session(engine) as first:
            original_refresh = first.refresh

            def refresh_then_start_status_edit(instance: object) -> None:
                nonlocal worker
                original_refresh(instance)
                worker = threading.Thread(target=edit_status, daemon=True)
                worker.start()
                assert started.wait(2)
                assert not finished.wait(0.5), "status edit passed the completion lock"

            with (
                patch.object(first, "refresh", side_effect=refresh_then_start_status_edit),
                patch("app.routers.admin.send_invitation_letter", return_value="resend-locked"),
                patch("app.routers.admin.poll_invitation_letter_delivery.apply_async"),
            ):
                sent = send_invitation_letter_endpoint(
                    InvitationLetterBody(email="race-after-recheck@example.com"),
                    session=first,
                    _=None,
                )
        assert finished.wait(5)
        assert errors == []
        with Session(engine) as check:
            current = check.get(WaitlistEntry, entry_id)
            invite = check.get(Invite, sent.invite_id)
            assert current is not None and current.status == "rejected"
            assert current.link_sent_at is None
            assert invite is not None and invite.letter_provider_message_id == "resend-locked"
    finally:
        if worker is not None:
            worker.join(timeout=5)
        with Session(engine) as cleanup:
            cleanup.execute(delete(Invite).where(Invite.waitlist_entry_id == entry_id))
            cleanup.execute(delete(WaitlistEntry).where(WaitlistEntry.id == entry_id))
            cleanup.commit()


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
    foreign_payload = f"other-unsubscribe-v1:{issued.id}:zh"
    signature = hmac.new(
        get_settings().APP_SECRET_KEY.get_secret_value().encode(),
        foreign_payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    foreign_token = (
        base64.urlsafe_b64encode(f"{foreign_payload}.{signature}".encode()).decode().rstrip("=")
    )
    assert verify_token(foreign_token) is None
    url = f"/invitation-letters/unsubscribe?token={token}"
    confirmation = app_client.get(url)
    assert confirmation.status_code == 200
    assert '<form method="post">' in confirmation.text
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
    before_waitlist = db_session.scalars(select(WaitlistEntry)).all()
    with (
        patch("app.services.email_sender.httpx.Client") as client_class,
        patch("app.routers.admin.poll_invitation_letter_delivery.apply_async") as poll,
    ):
        _resend_success(client_class, "resend-1")
        response = app_client.post(
            "/admin/invitation-letters",
            json={"email": "Letter@Example.com"},
            headers=_ops_headers(),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "letter@example.com"
        assert body["language"] == "en"
        assert body["waitlist_entry_id"] is None
        assert db_session.scalars(select(WaitlistEntry)).all() == before_waitlist
        row = db_session.get(Invite, uuid.UUID(body["invite_id"]))
        assert row is not None and row.waitlist_entry_id is None
        assert row.letter_provider_message_id == "resend-1" and row.letter_sent_at is not None
        assert body["provider_message_id"] == "resend-1"
        assert body["invite_url"].startswith(f"{get_settings().FRONTEND_URL}/signup?invite=")
        token = body["invite_url"].split("invite=", 1)[1]
        from app.services.invites import hash_invite_token

        assert row.token_hash == hash_invite_token(token)
        call = client_class.return_value.__enter__.return_value.post.call_args
        payload = call.kwargs["json"]
        assert payload["subject"] == "Portfonia Invitation Letter"
        assert payload["reply_to"] == get_settings().EMAIL_REPLY_TO
        assert payload["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        unsubscribe_url = payload["headers"]["List-Unsubscribe"].strip("<>")
        assert unsubscribe_url.startswith(
            f"{get_settings().FRONTEND_URL}/api/invitation-letters/unsubscribe?token="
        )
        assert verify_token(unsubscribe_url.split("token=", 1)[1]) == (row.id, "en")
        assert f'<a href="{body["invite_url"]}">{body["invite_url"]}</a>' in payload["html"]
        assert body["invite_url"] in payload["text"]
        assert call.kwargs["headers"]["Idempotency-Key"] == f"invitation-letter:{row.id}"
        poll.assert_called_once_with(args=[body["invite_id"]], countdown=600)

        row.letter_unsubscribed_at = row.letter_sent_at
        db_session.commit()
        before_invites = db_session.scalars(
            select(Invite).where(Invite.email == body["email"])
        ).all()
        blocked = app_client.post(
            "/admin/invitation-letters",
            json={"email": " LETTER@example.com "},
            headers=_ops_headers(),
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "recipient unsubscribed from invitation letters"
        assert (
            db_session.scalars(select(Invite).where(Invite.email == body["email"])).all()
            == before_invites
        )
        client_class.return_value.__enter__.return_value.post.assert_called_once()


def test_waitlist_locale_and_send_failure(app_client: TestClient, db_session: Session) -> None:
    entry = WaitlistEntry(email="waiting@example.com", locale="zh-Hant", status="pending")
    db_session.add(entry)
    db_session.commit()
    headers = {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}
    with (
        patch("app.services.email_sender.httpx.Client") as client_class,
        patch("app.routers.admin.poll_invitation_letter_delivery.apply_async") as poll,
    ):
        response_mock = client_class.return_value.__enter__.return_value.post.return_value
        response_mock.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "https://api.resend.com/emails"),
            response=httpx.Response(500),
        )
        response = app_client.post(
            "/admin/invitation-letters",
            json={"email": entry.email, "language": "en"},
            headers=headers,
        )
        assert response.status_code == 502
        assert (
            client_class.return_value.__enter__.return_value.post.call_args.kwargs["json"][
                "subject"
            ]
            == "Portfonia 邀请函"
        )
        poll.assert_not_called()
    db_session.refresh(entry)
    assert entry.status == "invited" and entry.link_sent_at is None
    old = db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == entry.id))
    assert old is not None and old.letter_sent_at is None and old.revoked_at is None
    with (
        patch("app.services.email_sender.httpx.Client") as client_class,
        patch("app.routers.admin.poll_invitation_letter_delivery.apply_async") as poll,
    ):
        _resend_success(client_class, "resend-zh")
        response = app_client.post(
            "/admin/invitation-letters", json={"email": entry.email}, headers=headers
        )
    assert response.status_code == 201 and response.json()["language"] == "zh"
    assert (
        client_class.return_value.__enter__.return_value.post.call_args.kwargs["json"]["subject"]
        == "Portfonia 邀请函"
    )
    db_session.refresh(entry)
    db_session.refresh(old)
    assert old.revoked_at is not None
    assert entry.link_sent_at is not None
    new_id = uuid.UUID(response.json()["invite_id"])
    new = db_session.get(Invite, new_id)
    assert new is not None and new.letter_sent_at is not None
    assert new.letter_provider_message_id == "resend-zh"
    live = db_session.scalars(
        select(Invite).where(
            Invite.waitlist_entry_id == entry.id,
            Invite.used_at.is_(None),
            Invite.revoked_at.is_(None),
            Invite.expires_at > datetime.now(tz=ET),
        )
    ).all()
    assert [invite.id for invite in live] == [new_id]
    assert app_client.get(f"/admin/waitlist/{entry.id}", headers=headers).json()["stage"] == "sent"
    poll.assert_called_once_with(args=[str(new_id)], countdown=600)


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
    before = db_session.scalars(select(Invite).where(Invite.email == entry.email)).all()
    with patch("app.routers.admin.send_invitation_letter") as send:
        response = app_client.post(
            "/admin/invitation-letters", json={"email": entry.email}, headers=headers
        )
    assert response.status_code == 409 and response.json()["detail"] == expected
    assert db_session.scalars(select(Invite).where(Invite.email == entry.email)).all() == before
    send.assert_not_called()


def test_existing_user_refused_without_new_invite(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, uuid.uuid4(), "taken-letter@example.com")
    db_session.commit()
    with patch("app.routers.admin.send_invitation_letter") as send:
        response = app_client.post(
            "/admin/invitation-letters",
            json={"email": "Taken-Letter@Example.com"},
            headers=_ops_headers(),
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "email already belongs to an existing user"
    assert (
        db_session.scalars(select(Invite).where(Invite.email == "taken-letter@example.com")).all()
        == []
    )
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
        patch("app.tasks.invitation_letter_tasks.get_settings") as settings,
        patch("app.tasks.invitation_letter_tasks.httpx.Client") as client_class,
        patch("app.tasks.invitation_letter_tasks.send_ops_alert") as alert,
    ):
        settings.return_value.RESEND_ALL_ACCESS_API_KEY = SecretStr("test-key")
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
            assert (
                f"Sent at (ET): {row.letter_sent_at.astimezone(ET).strftime('%Y-%m-%d %H:%M ET')}"
                in alert.call_args.kwargs["body"]
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


@pytest.mark.parametrize("provider_result", [{}, ValueError("bad JSON")])
def test_delivery_poll_retries_on_unusable_provider_response(
    db_session: Session, provider_result: object
) -> None:
    from app.tasks.invitation_letter_tasks import poll_invitation_letter_delivery

    issued = create_invite(db_session, created_by=uuid.uuid4(), email="malformed@example.com")
    row = db_session.get(Invite, issued.id)
    assert row is not None
    row.letter_provider_message_id = "resend-malformed"
    db_session.commit()
    with (
        patch("app.tasks.invitation_letter_tasks.get_settings") as settings,
        patch("app.tasks.invitation_letter_tasks.httpx.Client") as client_class,
        patch.object(
            poll_invitation_letter_delivery, "retry", side_effect=RuntimeError("retry")
        ) as retry,
    ):
        settings.return_value.RESEND_ALL_ACCESS_API_KEY = SecretStr("test-key")
        response = client_class.return_value.__enter__.return_value.get.return_value
        response.status_code = 200
        if isinstance(provider_result, Exception):
            response.json.side_effect = provider_result
        else:
            response.json.return_value = provider_result
        with pytest.raises(RuntimeError, match="retry"):
            poll_invitation_letter_delivery.run(str(issued.id))
        retry.assert_called_once()
    db_session.refresh(row)
    assert row.letter_delivery_event is None
