"""Unit tests for app/services/email_verification.py (issue #260, Ring
1-Email Validation design doc §3.2/§3.3).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import MagicMock

import pytest
from altcha import v1 as altcha_v1
from altcha.v1 import AlgoType
from sqlalchemy.orm import Session

from app.models.email_verification import EmailVerification
from app.models.user import User
from app.services.altcha_challenge import create_email_verification_challenge
from app.services.email_verification import (
    VERIFICATION_REJECTED_MESSAGE,
    VerificationRejected,
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
    second = create_verification(
        db_session, email="new2@example.com", purpose="delivery_email", user_id=_UID
    )

    db_session.expire_all()
    row1 = db_session.get(EmailVerification, first.id)
    row2 = db_session.get(EmailVerification, second.id)
    assert row1 is not None and row1.status == "superseded"
    assert row2 is not None and row2.status == "pending"


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
    second = create_verification(db_session, email="a@x.com", purpose="ops_manual", user_id=None)

    db_session.expire_all()
    row1 = db_session.get(EmailVerification, first.id)
    row2 = db_session.get(EmailVerification, second.id)
    assert row1 is not None and row1.status == "superseded"
    assert row2 is not None and row2.status == "pending"


def test_create_verification_failed_send_leaves_provider_message_id_null(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.email_verification.send_verification_email", MagicMock(return_value=None)
    )
    record = create_verification(
        db_session, email="a@example.com", purpose="ops_manual", user_id=None
    )
    assert record.status == "pending"  # the row still exists — send failure isn't fatal
    assert record.provider_message_id is None


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


def test_confirm_verification_writes_back_account_email(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(_user(_UID, "old@example.com"))
    db_session.flush()
    _record, token = _create_and_capture_token(
        db_session,
        monkeypatch,
        email="new-account@example.com",
        purpose="account_email",
        user_id=_UID,
    )

    confirm_verification(db_session, token=token, altcha_payload=_solved_altcha_payload())

    db_session.expire_all()
    user = db_session.get(User, _UID)
    assert user is not None
    assert user.email == "new-account@example.com"
    assert user.email_verified_at is not None


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
    _first, first_token = _create_and_capture_token(
        db_session, monkeypatch, email="new1@example.com", purpose="delivery_email", user_id=_UID
    )
    _second, _second_token = _create_and_capture_token(
        db_session, monkeypatch, email="new2@example.com", purpose="delivery_email", user_id=_UID
    )

    with pytest.raises(VerificationRejected):
        confirm_verification(db_session, token=first_token, altcha_payload=_solved_altcha_payload())
