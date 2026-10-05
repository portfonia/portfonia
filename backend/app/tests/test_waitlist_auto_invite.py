"""Daily automatic waitlist invitation letters (issue #672)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import ET, today_et
from app.models.invite import Invite
from app.models.waitlist_entry import WaitlistEntry
from app.services.invites import create_invite
from app.tasks import API_QUIET_BEAT_ENTRIES, celery_app
from app.tests.conftest import seed_user

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _limit(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    monkeypatch.setattr(get_settings(), "WAITLIST_AUTO_INVITE_DAILY_LIMIT", value)


def _entry(
    session: Session,
    email: str,
    *,
    status: str = "pending",
    locale: str = "en",
    created_at: datetime,
    link_sent_at: datetime | None = None,
) -> WaitlistEntry:
    row = WaitlistEntry(
        email=email,
        locale=locale,
        status=status,
        created_at=created_at,
        status_changed_at=created_at,
        link_sent_at=link_sent_at,
    )
    session.add(row)
    session.flush()
    return row


def _run() -> str:
    from app.tasks.waitlist_tasks import auto_invite_waitlist

    result = auto_invite_waitlist.run()
    assert isinstance(result, str)
    return result


def _alert() -> MagicMock:
    from app.tasks import waitlist_tasks

    alert = waitlist_tasks.send_ops_alert
    assert isinstance(alert, MagicMock)
    return alert


def _body() -> str:
    text = _alert().call_args.args[1]
    assert isinstance(text, str)
    return text


def _subject() -> str:
    text = _alert().call_args.args[0]
    assert isinstance(text, str)
    return text


def test_order_and_limit(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    _limit(monkeypatch, 2)
    base = datetime(2026, 10, 1, 9, tzinfo=ET)
    first = _entry(db_session, "t1@example.com", created_at=base)
    second = _entry(db_session, "t2@example.com", created_at=base + timedelta(hours=1))
    third = _entry(db_session, "t3@example.com", created_at=base + timedelta(hours=2))
    db_session.commit()
    from unittest.mock import patch

    with (
        patch(
            "app.services.invitation_letters.send_invitation_letter",
            side_effect=["resend-1", "resend-2"],
        ) as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    assert [call.args[0] for call in send.call_args_list] == [
        "t1@example.com",
        "t2@example.com",
    ]
    db_session.expire_all()
    for entry in (first, second):
        assert entry.status == "invited"
        assert entry.link_sent_at is not None
        invite = db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == entry.id))
        assert invite is not None
        assert invite.letter_sent_at is not None
        assert invite.waitlist_entry_id == entry.id
    assert third.status == "pending"
    assert db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == third.id)) is None
    body = _body()
    assert "Sent by this run: 2" in body
    assert "Still waiting on the waitlist: 1" in body


def test_global_quota_counts_letters_already_sent_today(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 2)
    today = today_et()
    yesterday = today - timedelta(days=1)
    today_letter = create_invite(
        db_session, created_by=uuid.uuid4(), email="manual-today@example.com"
    )
    older_letter = create_invite(
        db_session, created_by=uuid.uuid4(), email="manual-yesterday@example.com"
    )
    today_row = db_session.get(Invite, today_letter.id)
    older_row = db_session.get(Invite, older_letter.id)
    assert today_row is not None and older_row is not None
    today_row.letter_sent_at = datetime(today.year, today.month, today.day, 0, 30, tzinfo=ET)
    older_row.letter_sent_at = datetime(
        yesterday.year, yesterday.month, yesterday.day, 15, tzinfo=ET
    )
    base = datetime(2026, 10, 2, 9, tzinfo=ET)
    _entry(db_session, "q1@example.com", created_at=base)
    _entry(db_session, "q2@example.com", created_at=base + timedelta(hours=1))
    _entry(db_session, "q3@example.com", created_at=base + timedelta(hours=2))
    db_session.commit()
    from unittest.mock import patch

    with (
        patch(
            "app.services.invitation_letters.send_invitation_letter", return_value="resend-q"
        ) as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    assert send.call_count == 1
    assert send.call_args.args[0] == "q1@example.com"
    assert "Sent earlier today (before this run): 1" in _body()


def test_limit_zero_pauses_without_writes(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 0)
    base = datetime(2026, 10, 3, 9, tzinfo=ET)
    pending = _entry(db_session, "paused@example.com", created_at=base)
    expired = _entry(
        db_session,
        "paused-expired@example.com",
        status="invited",
        created_at=base + timedelta(hours=1),
        link_sent_at=base,
    )
    issued = create_invite(
        db_session,
        created_by=uuid.uuid4(),
        email=expired.email,
        waitlist_entry_id=expired.id,
        expires_days=1,
    )
    invite = db_session.get(Invite, issued.id)
    assert invite is not None
    invite.expires_at = datetime.now(tz=ET) - timedelta(days=1)
    invite.letter_sent_at = base
    db_session.commit()
    before_invites = db_session.scalar(select(func.count()).select_from(Invite))
    from unittest.mock import patch

    with (
        patch("app.services.invitation_letters.send_invitation_letter") as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "disabled"
    db_session.expire_all()
    assert send.call_count == 0
    assert _alert().call_count == 0
    assert pending.status == "pending" and pending.link_sent_at is None
    assert expired.status == "invited" and expired.link_sent_at == base
    assert db_session.scalar(select(func.count()).select_from(Invite)) == before_invites


def test_ineligible_pending_entries_need_attention(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 10)
    base = datetime(2026, 10, 4, 9, tzinfo=ET)
    seed_user(db_session, uuid.uuid4(), "member@example.com")
    _entry(db_session, "member@example.com", created_at=base)
    unsubscribed = _entry(db_session, "optout@example.com", created_at=base + timedelta(hours=1))
    issued = create_invite(
        db_session,
        created_by=uuid.uuid4(),
        email=unsubscribed.email,
        waitlist_entry_id=unsubscribed.id,
    )
    invite = db_session.get(Invite, issued.id)
    assert invite is not None
    invite.letter_unsubscribed_at = datetime.now(tz=ET)
    _entry(db_session, "eligible@example.com", created_at=base + timedelta(hours=2))
    db_session.commit()
    from unittest.mock import patch

    with (
        patch(
            "app.services.invitation_letters.send_invitation_letter", return_value="resend-ok"
        ) as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    assert [call.args[0] for call in send.call_args_list] == ["eligible@example.com"]
    body = _body()
    assert "member@example.com: already a registered user" in body
    assert "optout@example.com: unsubscribed from invitation letters" in body
    assert "Still waiting on the waitlist: 0" in body


def test_unconfirmed_send_counts_as_an_attempt(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 2)
    base = datetime(2026, 10, 5, 9, tzinfo=ET)
    first = _entry(db_session, "miss-1@example.com", created_at=base)
    second = _entry(db_session, "miss-2@example.com", created_at=base + timedelta(hours=1))
    third = _entry(db_session, "miss-3@example.com", created_at=base + timedelta(hours=2))
    db_session.commit()
    from unittest.mock import patch

    with (
        patch(
            "app.services.invitation_letters.send_invitation_letter",
            side_effect=[None, "resend-second"],
        ) as send,
        patch(
            "app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"
        ) as poll,
    ):
        assert _run() == "reported"
    assert send.call_count == 2
    db_session.expire_all()
    assert first.status == "invited" and first.link_sent_at is None
    first_invite = db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == first.id))
    assert first_invite is not None and first_invite.letter_sent_at is None
    assert second.status == "invited" and second.link_sent_at is not None
    assert third.status == "pending"
    body = _body()
    assert "Not confirmed (link created, the letter may or may not have gone out): 1" in body
    assert (
        "miss-1@example.com: today's automatic send was not confirmed; "
        "check Resend before resending"
    ) in body
    poll.assert_called_once()


def test_unexpected_send_exception_counts_as_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 2)
    base = datetime(2026, 10, 5, 11, tzinfo=ET)
    first = _entry(db_session, "boom-1@example.com", created_at=base)
    second = _entry(db_session, "boom-2@example.com", created_at=base + timedelta(hours=1))
    third = _entry(db_session, "boom-3@example.com", created_at=base + timedelta(hours=2))
    db_session.commit()
    from unittest.mock import patch

    with (
        patch(
            "app.services.invitation_letters.send_invitation_letter",
            side_effect=[RuntimeError("resend down"), "resend-second"],
        ) as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    assert send.call_count == 2
    db_session.expire_all()
    assert first.status == "invited" and first.link_sent_at is None
    first_invite = db_session.scalar(select(Invite).where(Invite.waitlist_entry_id == first.id))
    assert first_invite is not None and first_invite.letter_sent_at is None
    assert second.status == "invited" and second.link_sent_at is not None
    assert third.status == "pending"
    body = _body()
    assert "Not confirmed (link created, the letter may or may not have gone out): 1" in body
    assert (
        "boom-1@example.com: today's automatic send was not confirmed; "
        "check Resend before resending"
    ) in body


def test_nothing_to_report_skips_digest(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 10)
    from unittest.mock import patch

    with (
        patch("app.services.invitation_letters.send_invitation_letter") as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "nothing_to_report"
    assert send.call_count == 0
    assert _alert().call_count == 0


def test_attention_only_expired_link(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    _limit(monkeypatch, 10)
    sent_at = datetime.now(tz=ET) - timedelta(days=20)
    entry = _entry(
        db_session,
        "expired@example.com",
        status="invited",
        created_at=sent_at,
        link_sent_at=sent_at,
    )
    issued = create_invite(
        db_session, created_by=uuid.uuid4(), email=entry.email, waitlist_entry_id=entry.id
    )
    invite = db_session.get(Invite, issued.id)
    assert invite is not None
    invite.expires_at = datetime.now(tz=ET) - timedelta(days=1)
    invite.letter_sent_at = sent_at
    db_session.commit()
    from unittest.mock import patch

    with (
        patch("app.services.invitation_letters.send_invitation_letter") as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    assert send.call_count == 0
    body = _body()
    assert "Sent by this run: 0" in body
    assert "expired@example.com: link expired without signup" in body


def test_quota_used_up_with_nothing_to_flag(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 1)
    today = today_et()
    issued = create_invite(db_session, created_by=uuid.uuid4(), email="filled@example.com")
    invite = db_session.get(Invite, issued.id)
    assert invite is not None
    invite.letter_sent_at = datetime(today.year, today.month, today.day, 1, tzinfo=ET)
    waiting = _entry(
        db_session, "still-pending@example.com", created_at=datetime(2026, 10, 6, 9, tzinfo=ET)
    )
    db_session.commit()
    from unittest.mock import patch

    with (
        patch("app.services.invitation_letters.send_invitation_letter") as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "nothing_to_report"
    db_session.expire_all()
    assert send.call_count == 0
    assert _alert().call_count == 0
    assert waiting.status == "pending"


def test_bounced_letter_needs_attention_and_is_not_resent(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 10)
    sent_at = datetime.now(tz=ET) - timedelta(days=1)
    entry = _entry(
        db_session,
        "bounced@example.com",
        status="invited",
        created_at=sent_at,
        link_sent_at=sent_at,
    )
    issued = create_invite(
        db_session, created_by=uuid.uuid4(), email=entry.email, waitlist_entry_id=entry.id
    )
    invite = db_session.get(Invite, issued.id)
    assert invite is not None
    invite.letter_sent_at = sent_at
    invite.letter_delivery_event = "bounced"
    db_session.commit()
    from unittest.mock import patch

    with (
        patch("app.services.invitation_letters.send_invitation_letter") as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    assert send.call_count == 0
    assert "bounced@example.com: last invitation letter bounced" in _body()


def test_rejected_and_registered_are_excluded(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 10)
    base = datetime(2026, 9, 1, 9, tzinfo=ET)
    _entry(db_session, "rejected@example.com", status="rejected", created_at=base)
    registered = _entry(
        db_session,
        "registered@example.com",
        status="invited",
        created_at=base + timedelta(hours=1),
        link_sent_at=base,
    )
    issued = create_invite(
        db_session,
        created_by=uuid.uuid4(),
        email=registered.email,
        waitlist_entry_id=registered.id,
    )
    user = seed_user(db_session, uuid.uuid4(), registered.email)
    invite = db_session.get(Invite, issued.id)
    assert invite is not None
    invite.used_at = datetime.now(tz=ET)
    invite.used_by_user_id = user.id
    invite.letter_sent_at = base
    flagged = _entry(
        db_session,
        "flagged-expired@example.com",
        status="invited",
        created_at=base + timedelta(days=2),
        link_sent_at=base,
    )
    flagged_invite = create_invite(
        db_session, created_by=uuid.uuid4(), email=flagged.email, waitlist_entry_id=flagged.id
    )
    flagged_row = db_session.get(Invite, flagged_invite.id)
    assert flagged_row is not None
    flagged_row.expires_at = datetime.now(tz=ET) - timedelta(days=1)
    flagged_row.letter_sent_at = base
    db_session.commit()
    from unittest.mock import patch

    with (
        patch("app.services.invitation_letters.send_invitation_letter") as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    assert send.call_count == 0
    body = _body()
    assert "rejected@example.com" not in body
    assert "registered@example.com" not in body
    assert "flagged-expired@example.com: link expired without signup" in body


def test_status_reset_pending_entry_is_invited_again(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _limit(monkeypatch, 10)
    created_at = datetime(2026, 9, 2, 9, tzinfo=ET)
    entry = _entry(db_session, "reset@example.com", created_at=created_at)
    issued = create_invite(
        db_session, created_by=uuid.uuid4(), email=entry.email, waitlist_entry_id=entry.id
    )
    invite = db_session.get(Invite, issued.id)
    assert invite is not None
    invite.revoked_at = datetime.now(tz=ET) - timedelta(hours=1)
    db_session.commit()
    from unittest.mock import patch

    with (
        patch(
            "app.services.invitation_letters.send_invitation_letter", return_value="resend-reset"
        ),
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    db_session.expire_all()
    assert entry.status == "invited" and entry.link_sent_at is not None
    assert "link expired without signup" not in _body()
    assert "reset@example.com (English)" in _body()


def test_digest_format(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    _limit(monkeypatch, 1)
    base = datetime(2026, 9, 3, 9, tzinfo=ET)
    _entry(db_session, "english@example.com", locale="en", created_at=base)
    _entry(
        db_session,
        "simplified@example.com",
        locale="zh-Hans",
        created_at=base + timedelta(hours=1),
    )
    _entry(
        db_session,
        "traditional@example.com",
        locale="zh-Hant",
        created_at=base + timedelta(hours=2),
    )
    db_session.commit()
    today = today_et().isoformat()
    from unittest.mock import patch

    with (
        patch("app.services.invitation_letters.send_invitation_letter", return_value="resend-en"),
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    subject = _subject()
    body = _body()
    assert subject == f"Portfonia waitlist: 1 invitation letter sent on {today}"
    assert f"Daily waitlist invitations for {today} (ET)." in body
    assert "english@example.com (English)" in body
    assert _UUID_RE.search(subject) is None
    assert _UUID_RE.search(body) is None

    _limit(monkeypatch, 10)
    with (
        patch(
            "app.services.invitation_letters.send_invitation_letter",
            side_effect=["resend-zh", "resend-hant"],
        ),
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        assert _run() == "reported"
    later_subject = _subject()
    later_body = _body()
    assert later_subject == f"Portfonia waitlist: 2 invitation letters sent on {today}"
    assert "simplified@example.com (Simplified Chinese)" in later_body
    assert "traditional@example.com (Traditional Chinese)" in later_body
    assert f"Daily waitlist invitations for {today} (ET)." in later_body
    assert _UUID_RE.search(later_subject) is None
    assert _UUID_RE.search(later_body) is None


def test_waitlist_auto_invite_beat_schedule() -> None:
    entry = celery_app.conf.beat_schedule["waitlist-auto-invite-daily"]
    assert entry["task"] == "app.tasks.waitlist_tasks.auto_invite_waitlist"
    schedule = entry["schedule"]
    assert schedule.hour == {10}
    assert schedule.minute == {0}
    assert schedule.day_of_week == set(range(7))
    assert API_QUIET_BEAT_ENTRIES["waitlist-auto-invite-daily"] is False
