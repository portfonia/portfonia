"""Issue #566 public waitlist and ops flow on real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import cast
from unittest.mock import MagicMock

import pytest
from altcha import v1 as altcha_v1
from altcha.v1 import AlgoType
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import rate_limit
from app.core.config import get_settings
from app.core.timezones import ET
from app.models.invite import Invite
from app.models.user import User
from app.models.waitlist_entry import WaitlistEntry
from app.services.invites import hash_invite_token
from app.services.user_purge import purge_user
from app.tests.conftest import seed_user


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}


def _solution(client: TestClient, path: str = "/waitlist/altcha-challenge") -> str:
    response = client.get(path)
    assert response.status_code == 200
    challenge = response.json()
    algorithm = cast(AlgoType, challenge["algorithm"])
    solved = altcha_v1.solve_challenge(
        challenge=challenge["challenge"],
        salt=challenge["salt"],
        algorithm=algorithm,
        max_number=challenge["maxNumber"],
    )
    assert solved is not None
    return altcha_v1.Payload(
        algorithm=algorithm,
        challenge=challenge["challenge"],
        number=solved.number,
        salt=challenge["salt"],
        signature=challenge["signature"],
    ).to_base64()


def _submit(client: TestClient, email: str, locale: str = "zh-Hans") -> Response:
    return client.post(
        "/waitlist",
        json={"email": email, "locale": locale, "altcha": _solution(client)},
    )


def test_new_repeat_and_rejected_submission(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    notice = MagicMock()
    monkeypatch.setattr("app.routers.waitlist.send_admin_alert_task.delay", notice)
    first = _submit(app_client, " A@X.COM ")
    assert first.status_code == 200
    assert first.json() == {"received": True}
    row = db_session.scalar(select(WaitlistEntry).where(WaitlistEntry.email == "a@x.com"))
    assert row is not None and row.locale == "zh-Hans" and row.status == "pending"
    assert notice.call_args.args[0] == "Portfonia waitlist: new request from a@x.com"
    submitted_et = row.created_at.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    assert notice.call_args.args[1] == (
        "Someone joined the waitlist.\n\n"
        "Email: a@x.com\n"
        "Language: Simplified Chinese\n"
        f"Submitted: {submitted_et}\n\n"
        "Nothing has been done with this request yet."
    )
    assert notice.call_args.kwargs == {"severity": "INFO"}
    assert _submit(app_client, "a@x.com", "en").json() == {"received": True}
    db_session.refresh(row)
    assert row.locale == "zh-Hans"
    assert notice.call_args.args[0] == "Portfonia waitlist: a@x.com asked again"
    assert "Current status: not handled yet." in notice.call_args.args[1]
    row.status = "rejected"
    db_session.flush()
    assert _submit(app_client, "a@x.com").json() == {"received": True}
    db_session.refresh(row)
    assert row.status == "rejected"
    assert "rejected earlier. It stays rejected" in notice.call_args.args[1]
    assert notice.call_count == 3


def test_mint_sent_and_manual_status(app_client: TestClient, db_session: Session) -> None:
    assert _submit(app_client, "mint@example.com").status_code == 200
    row = db_session.scalar(select(WaitlistEntry).where(WaitlistEntry.email == "mint@example.com"))
    assert row is not None
    path = f"/admin/waitlist/{row.id}"
    assert app_client.post(f"{path}/sent", headers=_headers()).status_code == 409
    minted = app_client.post(f"{path}/invite", headers=_headers(), json={})
    assert minted.status_code == 200
    assert minted.json()["stage"] == "invited"
    assert minted.json()["invite_url"].endswith(f"/signup?invite={minted.json()['token']}")
    first_invite = db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == row.id))
    assert first_invite is not None and first_invite.email == row.email
    assert app_client.post(f"{path}/sent", headers=_headers()).json()["stage"] == "sent"
    reminted = app_client.post(f"{path}/invite", headers=_headers(), json={})
    assert reminted.status_code == 200
    db_session.refresh(first_invite)
    assert first_invite.revoked_at is not None
    live = db_session.scalars(
        select(Invite).where(
            Invite.waitlist_entry_id == row.id,
            Invite.used_at.is_(None),
            Invite.revoked_at.is_(None),
            Invite.expires_at > datetime.now(tz=ET),
        )
    ).all()
    assert len(live) == 1
    assert live[0].token_hash == hash_invite_token(reminted.json()["token"])
    assert reminted.json()["link_sent_at"] is None
    rejected = app_client.patch(f"{path}/status", headers=_headers(), json={"status": "rejected"})
    assert rejected.json()["stage"] == "rejected"
    assert rejected.json()["link_sent_at"] is None
    assert app_client.get(path, headers=_headers()).json()["link_expired"] is True


def test_purpose_distinct_and_validation(app_client: TestClient) -> None:
    assert (
        app_client.post(
            "/waitlist", json={"email": "a@x.com", "locale": "en", "altcha": "bad"}
        ).status_code
        == 400
    )
    assert (
        app_client.post(
            "/waitlist",
            json={
                "email": "a@x.com",
                "locale": "en",
                "altcha": _solution(app_client, "/auth/altcha-challenge"),
            },
        ).status_code
        == 400
    )
    assert (
        app_client.post(
            "/waitlist", json={"email": "  ", "locale": "en", "altcha": _solution(app_client)}
        ).status_code
        == 422
    )
    assert (
        app_client.get("/admin/waitlist", headers=_headers(), params={"stage": "bogus"}).status_code
        == 422
    )
    assert app_client.get(f"/admin/waitlist/{uuid.uuid4()}", headers=_headers()).status_code == 404


def test_existing_user_and_mint_conflict(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_user(db_session, uuid.uuid4(), "taken@example.com")
    notice = MagicMock()
    monkeypatch.setattr("app.routers.waitlist.send_admin_alert_task.delay", notice)
    assert _submit(app_client, "TAKEN@example.com").json() == {"received": True}
    assert notice.call_args.args[1].endswith(
        "Note: this email already belongs to a registered Portfonia account, "
        "so an invite link cannot be generated for it."
    )
    row = db_session.scalar(select(WaitlistEntry).where(WaitlistEntry.email == "taken@example.com"))
    assert row is not None
    assert (
        app_client.post(f"/admin/waitlist/{row.id}/invite", headers=_headers(), json={}).status_code
        == 409
    )
    assert db_session.scalars(select(Invite).where(Invite.waitlist_entry_id == row.id)).all() == []
    db_session.refresh(row)
    assert row.status == "pending"


def test_waitlist_rate_limits_and_redis_unavailable(app_client: TestClient) -> None:
    solution = _solution(app_client)
    for n in range(5):
        assert (
            app_client.post(
                "/waitlist",
                json={"email": f"ip{n}@example.com", "locale": "en", "altcha": solution},
            ).status_code
            == 200
        )
    assert (
        app_client.post(
            "/waitlist", json={"email": "ip5@example.com", "locale": "en", "altcha": solution}
        ).status_code
        == 429
    )


def test_waitlist_email_limit_and_hash(app_client: TestClient) -> None:
    solution = _solution(app_client)
    for _ in range(3):
        assert (
            app_client.post(
                "/waitlist", json={"email": "same@example.com", "locale": "en", "altcha": solution}
            ).status_code
            == 200
        )
    assert (
        app_client.post(
            "/waitlist", json={"email": "same@example.com", "locale": "en", "altcha": solution}
        ).status_code
        == 429
    )
    backend = rate_limit.get_backend()
    assert isinstance(backend, rate_limit.InMemoryBackend)
    assert all("same@example.com" not in key for key in backend.stored_keys())


def test_waitlist_redis_failure_is_503(app_client: TestClient) -> None:
    class Unavailable(rate_limit.InMemoryBackend):
        def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
            raise rate_limit.RateLimitUnavailable()

    solution = _solution(app_client)
    rate_limit.set_backend(Unavailable())
    assert (
        app_client.post(
            "/waitlist", json={"email": "a@example.com", "locale": "en", "altcha": solution}
        ).status_code
        == 503
    )


def test_signup_derived_progress_and_purge(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.routers.auth.create_auth_user", MagicMock(return_value="waitlist-sub"))
    assert _submit(app_client, "progress@example.com").status_code == 200
    entry = db_session.scalar(
        select(WaitlistEntry).where(WaitlistEntry.email == "progress@example.com")
    )
    assert entry is not None
    path = f"/admin/waitlist/{entry.id}"
    issued = app_client.post(f"{path}/invite", headers=_headers(), json={}).json()
    rejected = app_client.post(
        "/auth/signup",
        json={
            "invite_token": issued["token"],
            "email": "other@example.com",
            "password": "long-enough",
            "tos_accepted": True,
        },
    )
    assert rejected.status_code == 400
    signup = app_client.post(
        "/auth/signup",
        json={
            "invite_token": issued["token"],
            "email": "progress@example.com",
            "password": "long-enough",
            "tos_accepted": True,
        },
    )
    assert signup.status_code == 201
    registered = app_client.get(path, headers=_headers()).json()
    invite = db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == entry.id))
    assert invite is not None
    assert invite.used_at is not None
    assert registered["stage"] == "registered"
    assert registered["registered_at"] == invite.used_at.isoformat()
    registered_list = app_client.get(
        "/admin/waitlist", headers=_headers(), params={"stage": "registered"}
    ).json()
    assert [item["id"] for item in registered_list] == [str(entry.id)]
    assert app_client.post(f"{path}/invite", headers=_headers(), json={}).status_code == 409
    assert (
        app_client.patch(
            f"{path}/status", headers=_headers(), json={"status": "pending"}
        ).status_code
        == 409
    )
    user = db_session.get(User, signup.json()["id"])
    assert user is not None
    from datetime import datetime

    user.email_verified_at = datetime.now(tz=ET)
    db_session.flush()
    activated = app_client.get(path, headers=_headers()).json()
    assert activated["stage"] == "activated"
    assert activated["verified_at"] == activated["activated_at"]
    activated_list = app_client.get(
        "/admin/waitlist", headers=_headers(), params={"stage": "activated"}
    ).json()
    assert [item["id"] for item in activated_list] == [str(entry.id)]
    purge_user(db_session, user.id)
    db_session.flush()
    after_purge = app_client.get(path, headers=_headers()).json()
    assert after_purge["stage"] == "registered"
    assert after_purge["user_id"] is None
    assert after_purge["verified_at"] is None


def test_list_filters_and_expired_link(app_client: TestClient, db_session: Session) -> None:
    for email in ("pending@example.com", "sent@example.com", "rejected@example.com"):
        assert _submit(app_client, email).status_code == 200
    entries = {entry.email: entry for entry in db_session.scalars(select(WaitlistEntry)).all()}
    sent = entries["sent@example.com"]
    rejected = entries["rejected@example.com"]
    sent_path = f"/admin/waitlist/{sent.id}"
    app_client.post(f"{sent_path}/invite", headers=_headers(), json={})
    invited_list = app_client.get(
        "/admin/waitlist", headers=_headers(), params={"stage": "invited"}
    ).json()
    assert [item["id"] for item in invited_list] == [str(sent.id)]
    app_client.post(f"{sent_path}/sent", headers=_headers())
    app_client.patch(
        f"/admin/waitlist/{rejected.id}/status", headers=_headers(), json={"status": "rejected"}
    )
    for stage in ("pending", "sent", "rejected"):
        result = app_client.get("/admin/waitlist", headers=_headers(), params={"stage": stage})
        assert result.status_code == 200
        assert len(result.json()) == 1
        assert result.json()[0]["stage"] == stage
    assert app_client.get(
        "/admin/waitlist/by-email", headers=_headers(), params={"email": " SENT@example.com "}
    ).json()["id"] == str(sent.id)
    invite = db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == sent.id))
    assert invite is not None
    invite.expires_at = datetime.now(tz=ET) - timedelta(days=1)
    db_session.flush()
    expired = app_client.get(
        "/admin/waitlist", headers=_headers(), params={"link_expired": "true"}
    ).json()
    assert [item["id"] for item in expired] == [str(sent.id)]
    not_expired = app_client.get(
        "/admin/waitlist", headers=_headers(), params={"link_expired": "false"}
    ).json()
    assert {item["id"] for item in not_expired} == {
        str(entries["pending@example.com"].id),
        str(rejected.id),
    }
    assert app_client.post(f"{sent_path}/sent", headers=_headers()).status_code == 409
    reminted = app_client.post(f"{sent_path}/invite", headers=_headers(), json={})
    assert reminted.json()["stage"] == "invited"
    assert reminted.json()["link_sent_at"] is None


def test_mint_from_rejected_unrejects_and_manual_pending_revokes(
    app_client: TestClient, db_session: Session
) -> None:
    assert _submit(app_client, "reconsider@example.com").status_code == 200
    entry = db_session.scalar(
        select(WaitlistEntry).where(WaitlistEntry.email == "reconsider@example.com")
    )
    assert entry is not None
    path = f"/admin/waitlist/{entry.id}"
    assert (
        app_client.patch(f"{path}/status", headers=_headers(), json={"status": "rejected"}).json()[
            "stage"
        ]
        == "rejected"
    )
    minted = app_client.post(f"{path}/invite", headers=_headers(), json={})
    assert minted.json()["stage"] == "invited"
    invite = db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == entry.id))
    assert invite is not None
    pending = app_client.patch(f"{path}/status", headers=_headers(), json={"status": "pending"})
    assert pending.json()["stage"] == "pending"
    db_session.refresh(invite)
    assert invite.revoked_at is not None


def test_admin_invites_unmarked_and_waitlist_ops_auth(
    app_client: TestClient, db_session: Session
) -> None:
    assert app_client.get("/admin/waitlist").status_code == 401
    created = app_client.post("/admin/invites", headers=_headers(), json={})
    assert created.status_code == 201
    invite = db_session.get(Invite, created.json()["id"])
    assert invite is not None and invite.waitlist_entry_id is None
