"""Invite issuance and atomic redeem (Ring 1-B design.md §6.4, B-UAT-7/8)."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.invite import Invite
from app.services.invites import (
    INVITE_REJECTED_MESSAGE,
    InviteRejected,
    create_invite,
    hash_invite_token,
    list_invites,
    redeem_invite,
    revoke_invite,
)

_CREATOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


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
