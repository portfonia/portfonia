"""POST /auth/signup — invite gate + users insert, never auto-provision."""

from __future__ import annotations

import logging
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.invites import INVITE_REJECTED_MESSAGE, create_invite

_CREATOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.fixture
def _fake_auth_provider(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    create = MagicMock(return_value="supabase-sub-new")
    delete = MagicMock()
    monkeypatch.setattr("app.routers.auth.create_auth_user", create)
    monkeypatch.setattr("app.routers.auth.delete_auth_user", delete)
    return create


def test_signup_without_invite_rejected(
    app_client: TestClient, db_session: Session, _fake_auth_provider: MagicMock
) -> None:
    before = db_session.execute(select(User)).scalars().all()
    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": "no-such-token",
            "email": "new@example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == INVITE_REJECTED_MESSAGE
    db_session.expire_all()
    after = db_session.execute(select(User)).scalars().all()
    assert len(after) == len(before)
    _fake_auth_provider.assert_not_called()


def test_signup_with_valid_invite_creates_user(
    app_client: TestClient, db_session: Session, _fake_auth_provider: MagicMock
) -> None:
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()

    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": issued.token,
            "email": "New@Example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "new@example.com"
    assert "password" not in body

    row = db_session.execute(select(User).where(User.email == "new@example.com")).scalar_one()
    assert row.auth_subject == "supabase-sub-new"
    assert row.auth_provider == "supabase"
    assert row.status == "active"
    _fake_auth_provider.assert_called_once()


def test_signup_persists_weekly_cadence_and_tos_accepted_at(
    app_client: TestClient, db_session: Session, _fake_auth_provider: MagicMock
) -> None:
    """Ring 1-Onboarding.md §一.6: new users default to weekly, not mwf."""
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()

    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": issued.token,
            "email": "weekly@example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
        },
    )
    assert resp.status_code == 201

    row = db_session.execute(select(User).where(User.email == "weekly@example.com")).scalar_one()
    assert row.report_cadence == "weekly"
    assert row.tos_accepted_at is not None


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_signup_stores_locale_from_request_when_present(
    app_client: TestClient,
    db_session: Session,
    _fake_auth_provider: MagicMock,
    locale: str,
) -> None:
    """Issue #308: the frontend now forwards its UI locale, mapped to a bare
    backend code, at signup — the request's `locale` becomes users.locale
    when present and valid."""
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()

    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": issued.token,
            "email": f"locale-{locale}@example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
            "locale": locale,
        },
    )
    assert resp.status_code == 201

    row = db_session.execute(
        select(User).where(User.email == f"locale-{locale}@example.com")
    ).scalar_one()
    assert row.locale == locale


def test_signup_falls_back_to_zh_locale_when_absent(
    app_client: TestClient, db_session: Session, _fake_auth_provider: MagicMock
) -> None:
    """Defense-in-depth only (issue #308): the frontend change means this
    should not normally be hit, but an omitted `locale` must still land on
    the existing hardcoded "zh" default rather than erroring."""
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()

    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": issued.token,
            "email": "no-locale@example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
        },
    )
    assert resp.status_code == 201

    row = db_session.execute(select(User).where(User.email == "no-locale@example.com")).scalar_one()
    assert row.locale == "zh"


@pytest.mark.parametrize("payload_extra", [{}, {"tos_accepted": False}])
def test_signup_rejects_omitted_or_false_tos_accepted(
    app_client: TestClient,
    db_session: Session,
    _fake_auth_provider: MagicMock,
    payload_extra: dict[str, bool],
) -> None:
    """Ring 1-Onboarding.md §2.5: tos_accepted must be required true, never
    defaulted — omitting it or sending false must both be rejected, and no
    user row may be created."""
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    before = db_session.execute(select(User)).scalars().all()

    payload = {
        "invite_token": issued.token,
        "email": "no-tos@example.com",
        "password": "a-long-enough-password",
        **payload_extra,
    }
    resp = app_client.post("/auth/signup", json=payload)
    assert resp.status_code == 422
    db_session.expire_all()
    after = db_session.execute(select(User)).scalars().all()
    assert len(after) == len(before)
    _fake_auth_provider.assert_not_called()


def test_signup_deletes_auth_user_if_db_work_fails_after_create(
    app_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #183 review: compensation must run for any failure after Auth create,
    not only AuthProviderError/IntegrityError — otherwise the Auth user is
    orphaned and retry maps to a generic invalid invite."""
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    create = MagicMock(return_value="sub-to-compensate")
    delete = MagicMock()
    monkeypatch.setattr("app.routers.auth.create_auth_user", create)
    monkeypatch.setattr("app.routers.auth.delete_auth_user", delete)
    monkeypatch.setattr(
        "app.routers.auth.backfill_news_surfaced_before",
        MagicMock(side_effect=RuntimeError("db work failed")),
    )

    with pytest.raises(RuntimeError, match="db work failed"):
        app_client.post(
            "/auth/signup",
            json={
                "invite_token": issued.token,
                "email": "orphan@example.com",
                "password": "a-long-enough-password",
                "tos_accepted": True,
            },
        )
    delete.assert_called_once_with("sub-to-compensate")
    db_session.expire_all()
    assert (
        db_session.execute(
            select(User).where(User.email == "orphan@example.com")
        ).scalar_one_or_none()
        is None
    )


def test_signup_validation_error_does_not_echo_password(app_client: TestClient) -> None:
    """Public 422 must not echo the submitted password (`input` in Pydantic errors)."""
    password = "s3cret7"
    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": "x",
            "email": "a@example.com",
            "password": password,
        },
    )
    assert resp.status_code == 422
    assert password not in resp.text


def test_signup_validation_error_does_not_echo_password_alongside_missing_tos(
    app_client: TestClient,
) -> None:
    """A pydantic v2 'missing field' error's `input` is the whole request body,
    not just that field — so a second validation failure alongside a valid
    password (e.g. omitted tos_accepted) must not leak it either."""
    password = "a-long-enough-password"
    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": "x",
            "email": "a@example.com",
            "password": password,
            # tos_accepted omitted on purpose
        },
    )
    assert resp.status_code == 422
    assert password not in resp.text


def test_signup_rejected_invite_tags_failure_reason(
    app_client: TestClient,
    db_session: Session,
    _fake_auth_provider: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #225 bug 1: an invalid invite must be distinguishable in logs
    from an auth-provider/integrity fault, even though both map to the same
    client-facing INVITE_REJECTED_MESSAGE."""
    logging.getLogger("app.routers.auth").disabled = False
    with caplog.at_level("INFO"):
        resp = app_client.post(
            "/auth/signup",
            json={
                "invite_token": "no-such-token",
                "email": "new@example.com",
                "password": "a-long-enough-password",
                "tos_accepted": True,
            },
        )
    assert resp.status_code == 400
    # Asserting the formatted message, not a LogRecord attribute (PR #246
    # round 1 review): app/main.py's logging.basicConfig format string never
    # interpolates `extra=`, so a bug that put the tag only in `extra=`
    # would pass a `hasattr(record, ...)` check while never actually
    # reaching a production log line.
    messages = [r.getMessage() for r in caplog.records]
    assert any("signup_failure_reason=invite_rejected" in m for m in messages)


def test_signup_auth_provider_error_tags_failure_reason(
    app_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #225 bug 1: an AuthProviderError must be tagged distinctly from
    invite_rejected/integrity_error so ops can alert on it specifically —
    unlike invite_rejected, this is not expected background noise."""
    from app.services.auth_provider import AuthProviderError

    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    monkeypatch.setattr(
        "app.routers.auth.create_auth_user",
        MagicMock(side_effect=AuthProviderError("boom")),
    )
    logging.getLogger("app.routers.auth").disabled = False
    with caplog.at_level("INFO"):
        resp = app_client.post(
            "/auth/signup",
            json={
                "invite_token": issued.token,
                "email": "auth-fail@example.com",
                "password": "a-long-enough-password",
                "tos_accepted": True,
            },
        )
    assert resp.status_code == 400
    messages = [r.getMessage() for r in caplog.records]
    assert any("signup_failure_reason=auth_provider_error" in m for m in messages)


def test_signup_integrity_error_tags_failure_reason(
    app_client: TestClient,
    db_session: Session,
    _fake_auth_provider: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """issue #225 bug 1: IntegrityError must be tagged distinctly too."""
    from sqlalchemy.exc import IntegrityError

    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    monkeypatch.setattr(
        "app.routers.auth.backfill_news_surfaced_before",
        MagicMock(side_effect=IntegrityError("stmt", {}, Exception("dupe"))),
    )
    logging.getLogger("app.routers.auth").disabled = False
    with caplog.at_level("INFO"):
        resp = app_client.post(
            "/auth/signup",
            json={
                "invite_token": issued.token,
                "email": "integrity-fail@example.com",
                "password": "a-long-enough-password",
                "tos_accepted": True,
            },
        )
    assert resp.status_code == 400
    messages = [r.getMessage() for r in caplog.records]
    assert any("signup_failure_reason=integrity_error" in m for m in messages)


def test_signup_compensation_failure_sends_ops_alert(
    app_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #225 bug 2: if the compensating delete_auth_user() call itself
    fails, the only trace must not be a stray log line — send_ops_alert must
    fire so a human actually finds the resulting orphan."""
    from app.services.auth_provider import AuthProviderError

    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    monkeypatch.setattr("app.routers.auth.create_auth_user", MagicMock(return_value="sub-orphaned"))
    monkeypatch.setattr(
        "app.routers.auth.delete_auth_user",
        MagicMock(side_effect=AuthProviderError("compensation boom")),
    )
    monkeypatch.setattr(
        "app.routers.auth.backfill_news_surfaced_before",
        MagicMock(side_effect=RuntimeError("db work failed")),
    )
    alert = MagicMock()
    monkeypatch.setattr("app.routers.auth.send_ops_alert", alert)

    with pytest.raises(RuntimeError, match="db work failed"):
        app_client.post(
            "/auth/signup",
            json={
                "invite_token": issued.token,
                "email": "orphan2@example.com",
                "password": "a-long-enough-password",
                "tos_accepted": True,
            },
        )
    alert.assert_called_once()
    # A concrete, copy-pasteable command — not the literal "{id}" f-string
    # escape this originally emitted (PR #246 round 1 review). There is no
    # local users.id to recover after a failed compensation (rollback ran);
    # `sub` is the only identifier the orphan-purge path can use.
    assert "DELETE /admin/users/sub-orphaned?confirm=" in alert.call_args.kwargs["body"]


def test_signup_does_not_log_password(
    app_client: TestClient,
    db_session: Session,
    _fake_auth_provider: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    password = "super-secret-password-xyz"
    with caplog.at_level("DEBUG"):
        resp = app_client.post(
            "/auth/signup",
            json={
                "invite_token": issued.token,
                "email": "safe@example.com",
                "password": password,
                "tos_accepted": True,
            },
        )
    assert resp.status_code == 201
    assert password not in caplog.text


# --- §4.1 signup hook (issue #262) ---


def test_signup_enqueues_account_email_verification(
    app_client: TestClient,
    db_session: Session,
    _fake_auth_provider: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful signup must enqueue the account-email verification task
    after commit, with the new user's id — never synchronously in the
    request path (create_verification does a blocking Resend HTTP call,
    Ring 1-Profile Page.md §8.7)."""
    from app.tasks.email_verification_tasks import send_account_email_verification_task

    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    delay = MagicMock()
    monkeypatch.setattr(send_account_email_verification_task, "delay", delay)

    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": issued.token,
            "email": "hook@example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
        },
    )

    assert resp.status_code == 201
    row = db_session.execute(select(User).where(User.email == "hook@example.com")).scalar_one()
    delay.assert_called_once_with(str(row.id))


def test_signup_failure_does_not_enqueue_account_email_verification(
    app_client: TestClient,
    db_session: Session,
    _fake_auth_provider: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.email_verification_tasks import send_account_email_verification_task

    delay = MagicMock()
    monkeypatch.setattr(send_account_email_verification_task, "delay", delay)

    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": "no-such-token",
            "email": "hook-fail@example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
        },
    )

    assert resp.status_code == 400
    delay.assert_not_called()


def test_signup_returns_201_even_if_enqueue_raises(
    app_client: TestClient,
    db_session: Session,
    _fake_auth_provider: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker outage at enqueue time must not fail the signup response or
    trip the Auth-user compensation (PR #263 review): the account is fully
    created by the time the hook runs. The user just won't get an automatic
    verification email — recoverable only via the Ops API, since no
    email_verifications row exists for the Profile page to act on."""
    from app.tasks.email_verification_tasks import send_account_email_verification_task

    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    delay = MagicMock(side_effect=RuntimeError("broker down"))
    monkeypatch.setattr(send_account_email_verification_task, "delay", delay)

    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": issued.token,
            "email": "enqueue-fail@example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
        },
    )

    assert resp.status_code == 201
    row = db_session.execute(
        select(User).where(User.email == "enqueue-fail@example.com")
    ).scalar_one()
    assert row is not None
    _fake_auth_provider.assert_called_once()  # no compensation delete ran
    delay.assert_called_once()
