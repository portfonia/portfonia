"""Invite issuance and atomic redeem (Ring 1-B design.md §6.4, B-UAT-7/8)."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.invite import Invite
from app.models.user import User
from app.services.invites import (
    INVITE_REJECTED_MESSAGE,
    EmailAlreadyRegistered,
    InviteRejected,
    create_invite,
    hash_invite_token,
    list_invites,
    redeem_invite,
    revoke_invite,
    signup_email_taken,
)

_CREATOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _seed_user(user_id: uuid.UUID, email: str) -> User:
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
    )


def _future(days: int = 14) -> datetime:
    return datetime.now(tz=UTC) + timedelta(days=days)


def test_create_invite_persists_hash_not_plaintext(db_session: Session) -> None:
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()

    row = db_session.get(Invite, issued.id)
    assert row is not None
    assert issued.token
    assert row.token_hash == hash_invite_token(issued.token)
    assert issued.token not in (row.token_hash or "")
    assert row.used_at is None
    assert row.revoked_at is None


# --- issue #188: creation-time overlap check against users.email ---------------


def test_create_invite_rejects_email_of_existing_user(db_session: Session) -> None:
    db_session.add(_seed_user(uuid.uuid4(), "taken@example.com"))
    db_session.flush()

    with pytest.raises(EmailAlreadyRegistered):
        create_invite(db_session, created_by=_CREATOR, email="taken@example.com")


@pytest.mark.parametrize("variant", ["Taken@Example.COM", "  taken@example.com  "])
def test_create_invite_overlap_matches_normalized_variants(
    db_session: Session, variant: str
) -> None:
    """Same normalization as signup: case-insensitive, whitespace-stripped."""
    db_session.add(_seed_user(uuid.uuid4(), "taken@example.com"))
    db_session.flush()

    with pytest.raises(EmailAlreadyRegistered):
        create_invite(db_session, created_by=_CREATOR, email=variant)


def test_create_invite_overlap_check_ignores_user_status(db_session: Session) -> None:
    """Mirror the signup check exactly: it filters on email only, never on
    status — a disabled user's mailbox must block an invite the same way,
    otherwise the two checks can disagree about the same address."""
    row = _seed_user(uuid.uuid4(), "gone@example.com")
    row.status = "suspended"  # CHECK allows active/deleted/suspended
    db_session.add(row)
    db_session.flush()

    with pytest.raises(EmailAlreadyRegistered):
        create_invite(db_session, created_by=_CREATOR, email="gone@example.com")


def test_create_invite_allows_unregistered_email(db_session: Session) -> None:
    db_session.add(_seed_user(uuid.uuid4(), "someone-else@example.com"))
    db_session.flush()

    issued = create_invite(db_session, created_by=_CREATOR, email="fresh@example.com")
    db_session.flush()
    assert issued.email == "fresh@example.com"


def test_create_invite_generic_email_unaffected_by_overlap_check(
    db_session: Session,
) -> None:
    db_session.add(_seed_user(uuid.uuid4(), "active@example.com"))
    db_session.flush()

    issued = create_invite(db_session, created_by=_CREATOR, email=None)
    db_session.flush()
    assert issued.email is None


def test_create_invite_rejects_email_before_persisting_row(db_session: Session) -> None:
    """A rejected request must not leave a half-created invite behind."""
    from sqlalchemy import select

    db_session.add(_seed_user(uuid.uuid4(), "taken@example.com"))
    db_session.flush()

    with pytest.raises(EmailAlreadyRegistered):
        create_invite(db_session, created_by=_CREATOR, email="taken@example.com")
    remaining = list(
        db_session.execute(select(Invite).where(Invite.email == "taken@example.com")).scalars()
    )
    assert remaining == []


def test_creation_overlap_check_mirrors_signup_check(db_session: Session) -> None:
    """Mirror property (issue #188, PR #219 review): creation and signup must
    consult the SAME email-taken lookup — `signup_email_taken`, the one
    helper both call sites import — not two copies of the query that can
    drift (e.g. if auth.py ever adds a status filter)."""
    emails = ["a@example.com", "B@Example.com", "  c@example.com", "d@EXAMPLE.com"]
    users = [_seed_user(uuid.uuid4(), email.strip().lower()) for email in emails[:2]]
    db_session.add_all(users)
    db_session.flush()

    def creation_rejects(email: str) -> bool:
        try:
            create_invite(db_session, created_by=_CREATOR, email=email)
            return False
        except EmailAlreadyRegistered:
            return True

    def signup_would_reject(email: str) -> bool:
        # Replicates only routers/auth.py's normalization (strip().lower());
        # the lookup itself is the shared helper signup calls.
        normalized = email.strip().lower()
        return signup_email_taken(db_session, normalized)

    for email in [*emails, "unregistered@example.com"]:
        assert creation_rejects(email) == signup_would_reject(email), email


def test_signup_and_create_invite_share_one_lookup(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural half of the mirror lock: the live signup path must call
    the shared `signup_email_taken` helper, not its own forked query."""
    import app.routers.auth as auth_module

    calls: list[str] = []

    def spy(session: Session, email: str) -> bool:
        calls.append(email)
        return True  # pretend taken -> signup must reject before anything else

    monkeypatch.setattr(auth_module, "signup_email_taken", spy)

    resp = app_client.post(
        "/auth/signup",
        json={
            "invite_token": "any-token",
            "email": "mirror@example.com",
            "password": "a-long-enough-password",
            "tos_accepted": True,
        },
    )

    # The helper ran on signup's normalized input and short-circuited the
    # request into the generic rejection before any Auth-provider call.
    assert calls == ["mirror@example.com"]
    assert resp.status_code == 400


def test_redeem_invite_succeeds_once(db_session: Session) -> None:
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    new_user = uuid.uuid4()

    invite_id = redeem_invite(db_session, issued.token, used_by=new_user, email="a@example.com")
    db_session.flush()

    assert invite_id == issued.id
    row = db_session.get(Invite, issued.id)
    assert row is not None
    assert row.used_by_user_id == new_user
    assert row.used_at is not None


def test_redeem_invite_second_use_rejected(db_session: Session) -> None:
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    redeem_invite(db_session, issued.token, used_by=uuid.uuid4(), email="a@example.com")
    db_session.flush()

    with pytest.raises(InviteRejected, match=INVITE_REJECTED_MESSAGE):
        redeem_invite(db_session, issued.token, used_by=uuid.uuid4(), email="b@example.com")


@pytest.mark.parametrize(
    "setup",
    ["missing", "expired", "revoked", "email_mismatch"],
)
def test_redeem_invite_boundaries_share_one_error(db_session: Session, setup: str) -> None:
    """B-UAT-8: error text must not distinguish missing / used / expired / mismatch."""
    token = "not-a-real-token"
    email = "user@example.com"
    if setup != "missing":
        issued = create_invite(
            db_session,
            created_by=_CREATOR,
            email="bound@example.com" if setup == "email_mismatch" else None,
            expires_at=(
                datetime.now(tz=UTC) - timedelta(hours=1) if setup == "expired" else _future()
            ),
        )
        db_session.flush()
        token = issued.token
        if setup == "revoked":
            revoke_invite(db_session, issued.id)
            db_session.flush()

    with pytest.raises(InviteRejected) as exc_info:
        redeem_invite(db_session, token, used_by=uuid.uuid4(), email=email)
    assert str(exc_info.value) == INVITE_REJECTED_MESSAGE


def test_list_invites_never_includes_token(db_session: Session) -> None:
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    listed = list_invites(db_session)
    assert listed
    assert all(getattr(item, "token", None) is None for item in listed)
    assert issued.token not in str(listed)


def test_revoke_invite_blocks_later_redeem(db_session: Session) -> None:
    issued = create_invite(db_session, created_by=_CREATOR)
    db_session.flush()
    revoke_invite(db_session, issued.id)
    db_session.flush()
    with pytest.raises(InviteRejected, match=INVITE_REJECTED_MESSAGE):
        redeem_invite(db_session, issued.token, used_by=uuid.uuid4(), email="a@example.com")


def test_concurrent_redeem_succeeds_exactly_once(session_test_db: None) -> None:
    """B-UAT-7: two racing redeemers, one token — exactly one used_by_user_id."""
    token_holder: dict[str, str] = {}
    invite_id_holder: dict[str, uuid.UUID] = {}

    with SessionLocal() as setup:
        issued = create_invite(setup, created_by=_CREATOR)
        setup.commit()
        token_holder["token"] = issued.token
        invite_id_holder["id"] = issued.id

    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            with SessionLocal() as session:
                redeem_invite(
                    session,
                    token_holder["token"],
                    used_by=uuid.uuid4(),
                    email=f"{uuid.uuid4().hex}@example.com",
                )
                session.commit()
            ok = True
        except InviteRejected:
            ok = False
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == 1

    with SessionLocal() as check:
        row = check.get(Invite, invite_id_holder["id"])
        assert row is not None
        assert row.used_by_user_id is not None
        used = check.execute(
            select(Invite.used_by_user_id).where(Invite.id == invite_id_holder["id"])
        ).scalar_one()
        assert used == row.used_by_user_id
        check.delete(row)
        check.commit()
