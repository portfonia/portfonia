"""Unit tests for app/services/email_verification.py (issue #260, Ring
1-Email Validation design doc §3.2/§3.3).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import MagicMock

import pytest
from altcha import v1 as altcha_v1
from altcha.v1 import AlgoType
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.email_verification import EmailVerification
from app.models.user import User
from app.services.altcha_challenge import create_email_verification_challenge
from app.services.email_verification import (
    RESEND_COOLDOWN,
    VERIFICATION_REJECTED_MESSAGE,
    ResendTooSoon,
    VerificationRejected,
    VerificationSendFailed,
    confirm_verification,
    create_verification,
    get_verification_status,
)

_UID = uuid.UUID("00000000-0000-0000-0000-0000000000e1")


def _user(user_id: uuid.UUID, email: str, delivery_email: str | None = None) -> User:
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        delivery_email=delivery_email,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
    )


def _solved_altcha_payload() -> str:
    """Same construction as test_auth_forgot_password.py's _solved_altcha,
    without an app_client — this file exercises the service layer directly."""
    challenge = create_email_verification_challenge()
    algorithm = cast(AlgoType, challenge["algorithm"])
    solution = altcha_v1.solve_challenge(
        challenge=cast(str, challenge["challenge"]),
        salt=cast(str, challenge["salt"]),
        algorithm=algorithm,
        max_number=cast(int, challenge["maxNumber"]),
    )
    assert solution is not None
    payload = altcha_v1.Payload(
        algorithm=algorithm,
        challenge=cast(str, challenge["challenge"]),
        number=solution.number,
        salt=cast(str, challenge["salt"]),
        signature=cast(str, challenge["signature"]),
    )
    return payload.to_base64()


def _age_past_cooldown(record: EmailVerification, db_session: Session) -> None:
    """Push a record's last_sent_at back past RESEND_COOLDOWN so a
    subsequent create_verification() call for the same scope supersedes it
    instead of raising ResendTooSoon — isolates "does superseding work" from
    "does the cooldown work" (the two failure modes create_verification
    checks together, in that order)."""
    record.last_sent_at = datetime.now(UTC) - RESEND_COOLDOWN - timedelta(seconds=1)
    db_session.commit()


def _create_and_capture_token(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    email: str,
    purpose: str,
    user_id: uuid.UUID | None = None,
) -> tuple[EmailVerification, str]:
    """create_verification() deliberately never returns the plaintext token
    (§3.2's hash-only discipline) — tests that need to drive the confirm
    flow capture it off the mocked send call instead, the only place it
    ever exists as plaintext."""
    sender = MagicMock(return_value="test-provider-id")
    monkeypatch.setattr("app.services.email_verification.send_verification_email", sender)
    record = create_verification(db_session, email=email, purpose=purpose, user_id=user_id)
    token = sender.call_args.args[1]
    return record, token


def test_create_verification_sends_email_and_creates_pending_row(db_session: Session) -> None:
    record = create_verification(
        db_session, email="a@example.com", purpose="ops_manual", user_id=None
    )
    assert record.status == "pending"
    assert record.email == "a@example.com"
    assert record.provider_message_id == "test-provider-id"  # autouse fixture's fake success
    assert record.user_id is None


def test_create_verification_supersedes_prior_pending_same_user_and_purpose(
    db_session: Session,
) -> None:
    db_session.add(_user(_UID, "seed@example.com"))
    db_session.flush()

    first = create_verification(
        db_session, email="new1@example.com", purpose="delivery_email", user_id=_UID
    )
    _age_past_cooldown(first, db_session)
    second = create_verification(
        db_session, email="new2@example.com", purpose="delivery_email", user_id=_UID
    )

    db_session.expire_all()
    row1 = db_session.get(EmailVerification, first.id)
    row2 = db_session.get(EmailVerification, second.id)
    assert row1 is not None and row1.status == "superseded"
    assert row2 is not None and row2.status == "pending"


def test_create_verification_raises_resend_too_soon_within_cooldown(
    db_session: Session,
) -> None:
    """§3.4 / issue #260 Notes: a caller (today, only the Ops API)
    create-and-sending in a tight loop must not supersede-and-resend on
    every call."""
    create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)

    with pytest.raises(ResendTooSoon):
        create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)


def test_create_verification_normalizes_email(db_session: Session) -> None:
    record = create_verification(
        db_session, email="  Mixed-Case@Example.com  ", purpose="ops_manual", user_id=None
    )
    assert record.email == "mixed-case@example.com"


def test_create_verification_rejects_blank_email(db_session: Session) -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        create_verification(db_session, email="   ", purpose="ops_manual", user_id=None)


def test_create_verification_ops_manual_probes_of_different_emails_do_not_supersede(
    db_session: Session,
) -> None:
    """Regression: purpose=ops_manual always has user_id=None (§3.5 — a
    bound Ops call passes account_email/delivery_email instead), so scoping
    supersede by (user_id, purpose) alone would group every unbound probe
    together regardless of address. Probing b@y.com must not retire a still-
    pending probe of a@x.com."""
    probe_a = create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)
    probe_b = create_verification(db_session, email="b@y.com", purpose="ops_manual", user_id=None)

    db_session.expire_all()
    row_a = db_session.get(EmailVerification, probe_a.id)
    row_b = db_session.get(EmailVerification, probe_b.id)
    assert row_a is not None and row_a.status == "pending"
    assert row_b is not None and row_b.status == "pending"


def test_create_verification_ops_manual_reprobe_of_same_email_supersedes_prior(
    db_session: Session,
) -> None:
    first = create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)
    _age_past_cooldown(first, db_session)
    second = create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)

    db_session.expire_all()
    row1 = db_session.get(EmailVerification, first.id)
    row2 = db_session.get(EmailVerification, second.id)
    assert row1 is not None and row1.status == "superseded"
    assert row2 is not None and row2.status == "pending"


def test_create_verification_raises_on_failed_send_and_touches_nothing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (review, PR #261 round 2): an earlier version persisted
    (superseded the prior row + stamped last_sent_at) BEFORE attempting the
    send, so a failed send destroyed a still-working prior link and started
    the resend cooldown on an email that was never sent. Now the send
    happens first — on failure, no row is created at all."""
    monkeypatch.setattr(
        "app.services.email_verification.send_verification_email", MagicMock(return_value=None)
    )

    with pytest.raises(VerificationSendFailed):
        create_verification(db_session, email="a@example.com", purpose="ops_manual", user_id=None)

    rows = (
        db_session.execute(
            select(EmailVerification).where(EmailVerification.email == "a@example.com")
        )
        .scalars()
        .all()
    )
    assert rows == []


def test_create_verification_failed_send_does_not_destroy_a_working_prior_link(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The specific bug this ordering fix closes: a prior pending record
    (still within its TTL, a real working link) must survive a failed
    resend attempt untouched — not get marked superseded before the send is
    even known to have worked."""
    first = create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)
    _age_past_cooldown(first, db_session)

    monkeypatch.setattr(
        "app.services.email_verification.send_verification_email", MagicMock(return_value=None)
    )
    with pytest.raises(VerificationSendFailed):
        create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)

    db_session.expire_all()
    row = db_session.get(EmailVerification, first.id)
    assert row is not None and row.status == "pending"  # untouched by the failed retry


def test_create_verification_failed_send_does_not_start_the_cooldown(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A send that never happened must not block an immediate real retry."""
    sender = MagicMock(return_value=None)
    monkeypatch.setattr("app.services.email_verification.send_verification_email", sender)
    with pytest.raises(VerificationSendFailed):
        create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)

    # A real retry right after must succeed, not hit ResendTooSoon — there
    # is nothing pending to collide with, since the failed attempt above
    # created no row at all.
    sender.return_value = "test-provider-id"
    record = create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)
    assert record.status == "pending"


def test_create_verification_resolves_locale_from_bound_user(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(_user(_UID, "seed@example.com"))
    db_session.flush()
    sender = MagicMock(return_value="test-provider-id")
    monkeypatch.setattr("app.services.email_verification.send_verification_email", sender)

    create_verification(db_session, email="new@example.com", purpose="delivery_email", user_id=_UID)

    assert sender.call_args.kwargs["locale"] == "zh"  # _user()'s fixture default


def test_create_verification_defaults_locale_to_en_for_unbound_probe(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = MagicMock(return_value="test-provider-id")
    monkeypatch.setattr("app.services.email_verification.send_verification_email", sender)

    create_verification(db_session, email="a@example.com", purpose="ops_manual", user_id=None)

    assert sender.call_args.kwargs["locale"] == "en"


def test_get_verification_status_not_found(db_session: Session) -> None:
    result = get_verification_status(db_session, token="does-not-exist")
    assert result.found is False
    assert result.status is None


def test_get_verification_status_pending(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record, token = _create_and_capture_token(
        db_session, monkeypatch, email="a@example.com", purpose="ops_manual"
    )
    result = get_verification_status(db_session, token=token)
    assert result.found is True
    assert result.status == "pending"
    assert result.email == "a@example.com"


def test_get_verification_status_reports_expired_without_persisting_it(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET must stay side-effect-free (design doc §3.3 step 2 / Vigil
    §4.2) — an expired row is reported as "expired" but its DB status
    column stays "pending" until an actual confirm attempt touches it."""
    record, token = _create_and_capture_token(
        db_session, monkeypatch, email="a@example.com", purpose="ops_manual"
    )
    record.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    result = get_verification_status(db_session, token=token)
    assert result.status == "expired"

    db_session.expire_all()
    row = db_session.get(EmailVerification, record.id)
    assert row is not None and row.status == "pending"  # untouched by the GET


def test_confirm_verification_writes_back_delivery_email_and_verified_at(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(_user(_UID, "seed@example.com"))
    db_session.flush()
    _record, token = _create_and_capture_token(
        db_session,
        monkeypatch,
        email="new-delivery@example.com",
        purpose="delivery_email",
        user_id=_UID,
    )

    confirmed = confirm_verification(
        db_session, token=token, altcha_payload=_solved_altcha_payload()
    )

    assert confirmed.status == "verified"
    assert confirmed.verified_at is not None
    db_session.expire_all()
    user = db_session.get(User, _UID)
    assert user is not None
    assert user.delivery_email == "new-delivery@example.com"
    assert user.delivery_email_verified_at is not None
    assert user.email_verified_at is None  # only the target field is touched


def test_confirm_verification_marks_account_email_verified_without_changing_it(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review, PR #261: account_email confirms reachability of the address
    already on `users.email` — it must never overwrite it. `users.email`
    is Supabase Auth's login identity; a flow that changes it locally
    without also updating Auth would desync sign-in from the local row.
    §4.1: this section adds only a reachability check of the account
    email, and does not touch the account email itself."""
    db_session.add(_user(_UID, "current@example.com"))
    db_session.flush()
    _record, token = _create_and_capture_token(
        db_session,
        monkeypatch,
        email="current@example.com",  # matches the account's real email
        purpose="account_email",
        user_id=_UID,
    )

    confirm_verification(db_session, token=token, altcha_payload=_solved_altcha_payload())

    db_session.expire_all()
    user = db_session.get(User, _UID)
    assert user is not None
    assert user.email == "current@example.com"  # untouched
    assert user.email_verified_at is not None


def test_confirm_verification_rejects_account_email_mismatch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record whose candidate address no longer matches the account's
    real email (stale record, or a caller error pairing the wrong user_id)
    must be rejected, not silently accepted or written anywhere."""
    db_session.add(_user(_UID, "current@example.com"))
    db_session.flush()
    _record, token = _create_and_capture_token(
        db_session,
        monkeypatch,
        email="someone-elses-guess@example.com",
        purpose="account_email",
        user_id=_UID,
    )

    with pytest.raises(VerificationRejected) as exc_info:
        confirm_verification(db_session, token=token, altcha_payload=_solved_altcha_payload())
    assert str(exc_info.value) == VERIFICATION_REJECTED_MESSAGE

    db_session.expire_all()
    user = db_session.get(User, _UID)
    assert user is not None
    assert user.email == "current@example.com"
    assert user.email_verified_at is None


def test_confirm_verification_ops_manual_has_no_write_back_target(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record, token = _create_and_capture_token(
        db_session, monkeypatch, email="a@example.com", purpose="ops_manual", user_id=None
    )

    confirmed = confirm_verification(
        db_session, token=token, altcha_payload=_solved_altcha_payload()
    )

    assert confirmed.status == "verified"  # the probe itself still records success


def test_confirm_verification_rejects_bad_altcha(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record, token = _create_and_capture_token(
        db_session, monkeypatch, email="a@example.com", purpose="ops_manual"
    )

    with pytest.raises(VerificationRejected) as exc_info:
        confirm_verification(db_session, token=token, altcha_payload="not-a-real-solution")
    assert str(exc_info.value) == VERIFICATION_REJECTED_MESSAGE


def test_confirm_verification_rejects_unknown_token(db_session: Session) -> None:
    with pytest.raises(VerificationRejected):
        confirm_verification(
            db_session, token="does-not-exist", altcha_payload=_solved_altcha_payload()
        )


def test_confirm_verification_rejects_second_use_of_same_token(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record, token = _create_and_capture_token(
        db_session, monkeypatch, email="a@example.com", purpose="ops_manual"
    )
    confirm_verification(db_session, token=token, altcha_payload=_solved_altcha_payload())

    with pytest.raises(VerificationRejected):
        confirm_verification(db_session, token=token, altcha_payload=_solved_altcha_payload())


def test_confirm_verification_rejects_and_persists_expired_status(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, token = _create_and_capture_token(
        db_session, monkeypatch, email="a@example.com", purpose="ops_manual"
    )
    record.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    with pytest.raises(VerificationRejected):
        confirm_verification(db_session, token=token, altcha_payload=_solved_altcha_payload())

    db_session.expire_all()
    row = db_session.get(EmailVerification, record.id)
    assert row is not None and row.status == "expired"  # POST DOES persist it, unlike GET


def test_confirm_verification_rejects_superseded_token(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(_user(_UID, "seed@example.com"))
    db_session.flush()
    first, first_token = _create_and_capture_token(
        db_session, monkeypatch, email="new1@example.com", purpose="delivery_email", user_id=_UID
    )
    _age_past_cooldown(first, db_session)
    _second, _second_token = _create_and_capture_token(
        db_session, monkeypatch, email="new2@example.com", purpose="delivery_email", user_id=_UID
    )

    with pytest.raises(VerificationRejected):
        confirm_verification(db_session, token=first_token, altcha_payload=_solved_altcha_payload())


def test_confirm_verification_rejects_when_record_superseded_mid_flight(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (review, PR #261 round 3): the final `pending`->`verified`
    write is a conditional `UPDATE ... WHERE status='pending'`, not a plain
    attribute assignment on the row read at the top of the function — this
    proves the guard actually fires when the row moves off `pending`
    between that initial read and the write, and critically, that the
    `users.delivery_email` write-back never happens when it does.

    A real concurrent request can't be driven from a single synchronous
    test, so the "concurrent" supersede is injected via monkeypatching
    `_target_field` — called by `confirm_verification` in exactly the
    window between the initial SELECT and the final conditional UPDATE —
    to perform the race as a side effect before returning its real value.
    """
    db_session.add(_user(_UID, "seed@example.com"))
    db_session.flush()
    record, token = _create_and_capture_token(
        db_session, monkeypatch, email="new@example.com", purpose="delivery_email", user_id=_UID
    )

    import app.services.email_verification as ev_module

    real_target_field = ev_module._target_field

    def _target_field_with_concurrent_supersede(purpose: str) -> str | None:
        db_session.execute(
            update(EmailVerification)
            .where(EmailVerification.id == record.id)
            .values(status="superseded")
        )
        db_session.commit()
        return real_target_field(purpose)

    monkeypatch.setattr(ev_module, "_target_field", _target_field_with_concurrent_supersede)

    with pytest.raises(VerificationRejected):
        confirm_verification(db_session, token=token, altcha_payload=_solved_altcha_payload())

    db_session.expire_all()
    user = db_session.get(User, _UID)
    assert user is not None
    assert user.delivery_email is None  # never written — the race was caught
    row = db_session.get(EmailVerification, record.id)
    assert row is not None and row.status == "superseded"  # not resurrected to verified


def test_create_verification_logs_when_persist_fails_after_send(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (review, PR #261 round 3): a persist failure AFTER a
    successful send must be logged loudly — the email is already out and
    the link is dead, so a naive retry would send a SECOND email, not
    resend this one. Uses a fully mocked session (not the db_session
    fixture) to trigger the failure deterministically without leaving a
    real Postgres transaction in an aborted state for fixture teardown to
    trip over."""
    # alembic/env.py's fileConfig() (run once per session by session_test_db,
    # which has already run by the time this test executes) disables any
    # logger instantiated before that call — this module's logger included,
    # since other test files import it at collection time. Documented
    # workaround (CLAUDE.md's Tests section): re-enable right before
    # asserting on caplog.
    logging.getLogger("app.services.email_verification").disabled = False
    caplog.set_level(logging.ERROR, logger="app.services.email_verification")
    monkeypatch.setattr(
        "app.services.email_verification.send_verification_email",
        MagicMock(return_value="test-provider-id"),
    )
    mock_session = MagicMock()
    mock_session.execute.return_value.scalar_one_or_none.return_value = None  # no prior pending
    mock_session.commit.side_effect = Exception("db down")

    with pytest.raises(Exception, match="db down"):
        create_verification(mock_session, email="a@example.com", purpose="ops_manual", user_id=None)

    assert any("failed to persist" in r.message for r in caplog.records)


def test_create_verification_swallows_poll_scheduling_failure(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (review, PR #261 round 3): the delivery-status poll is
    best-effort (design doc §3.3 step 6 is diagnostic, not load-bearing) —
    a Celery/Redis outage when scheduling it must not turn an otherwise-
    successful create+send+persist into an error."""
    monkeypatch.setattr(
        "app.tasks.email_verification_tasks.poll_email_verification_delivery.apply_async",
        MagicMock(side_effect=Exception("broker down")),
    )

    record = create_verification(
        db_session, email="a@example.com", purpose="ops_manual", user_id=None
    )

    assert record.status == "pending"
