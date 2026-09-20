"""services/vigil/objects.py — POST /vigil/objects/init and
POST /vigil/objects/upload business logic (issue #454, Vigil R0 P2.1).

Real Postgres per this project's test convention.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import VigilObject, VigilVault
from app.services.vigil.configuration import (
    validate_configuration_input,
    write_pending_configuration,
)
from app.services.vigil.objects import (
    VigilObjectConflict,
    VigilObjectInputError,
    VigilObjectNotFound,
    VigilRevisionConflict,
    init_object,
    upload_object,
)
from app.tests.vigil_helpers import ensure_confirmation_email

_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f2")


@pytest.fixture(autouse=True)
def _vigil_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VIGIL_NOTIFICATION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()


def _owner(db_session: Session) -> None:
    db_session.add(
        User(
            id=_OWNER_ID,
            auth_provider="supabase",
            auth_subject="owner-sub",
            email="owner@example.com",
            status="active",
            locale="zh",
            base_currency="USD",
            report_cadence="mwf",
            email_verified_at=datetime.now(UTC),
        )
    )
    db_session.flush()


def _seed_pending_config(db_session: Session) -> tuple[uuid.UUID, int]:
    """Returns (config_id, vault_revision_after_config_write)."""
    _owner(db_session)
    result = write_pending_configuration(
        db_session,
        owner_user_id=_OWNER_ID,
        confirmation_email_id=ensure_confirmation_email(db_session, owner_user_id=_OWNER_ID).id,
        expected_revision=0,
        normalized=validate_configuration_input(
            interval_days=30,
            grace_hours=72,
            recipients=[{"email": "a@example.com", "email_confirm": "a@example.com"}],
            message="",
        ),
    )
    db_session.flush()
    return result.config_id, result.revision


# --- init_object -----------------------------------------------------------


def test_init_creates_staging_object_and_bumps_revision(db_session: Session) -> None:
    config_id, revision = _seed_pending_config(db_session)
    result = init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        config_id=config_id,
        request_id=uuid.uuid4(),
        filename="will.pdf",
        plaintext_size=1000,
    )
    db_session.flush()
    assert result.created is True
    obj = db_session.get(VigilObject, result.object_id)
    assert obj is not None
    assert obj.status == "staging"
    assert obj.plaintext_size == 1000
    vault = db_session.get(VigilVault, result.vault_id)
    assert vault is not None
    assert vault.pending_object_id == obj.id
    assert vault.revision == revision + 1
    assert result.revision == revision + 1


def test_init_replay_same_request_id_and_input_is_idempotent(db_session: Session) -> None:
    config_id, revision = _seed_pending_config(db_session)
    request_id = uuid.uuid4()
    first = init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        config_id=config_id,
        request_id=request_id,
        filename="will.pdf",
        plaintext_size=1000,
    )
    db_session.flush()

    replay = init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,  # deliberately the STALE pre-write value
        config_id=config_id,
        request_id=request_id,
        filename="will.pdf",
        plaintext_size=1000,
    )
    assert replay.created is False
    assert replay.object_id == first.object_id
    assert replay.revision == first.revision


def test_init_replay_with_changed_input_conflicts(db_session: Session) -> None:
    config_id, revision = _seed_pending_config(db_session)
    request_id = uuid.uuid4()
    init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        config_id=config_id,
        request_id=request_id,
        filename="will.pdf",
        plaintext_size=1000,
    )
    db_session.flush()

    with pytest.raises(VigilObjectConflict):
        init_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            config_id=config_id,
            request_id=request_id,
            filename="different.pdf",
            plaintext_size=1000,
        )


def test_init_replay_after_superseded_conflicts(db_session: Session) -> None:
    config_id, revision = _seed_pending_config(db_session)
    old_request_id = uuid.uuid4()
    first = init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        config_id=config_id,
        request_id=old_request_id,
        filename="will.pdf",
        plaintext_size=1000,
    )
    db_session.flush()

    # A new init call supersedes the old pending object.
    init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=first.revision,
        config_id=config_id,
        request_id=uuid.uuid4(),
        filename="will-v2.pdf",
        plaintext_size=2000,
    )
    db_session.flush()

    old = db_session.get(VigilObject, first.object_id)
    assert old is not None and old.status == "retired"

    with pytest.raises(VigilObjectConflict):
        init_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=first.revision,
            config_id=config_id,
            request_id=old_request_id,
            filename="will.pdf",
            plaintext_size=1000,
        )


def test_init_superseding_a_ready_pending_nulls_its_ciphertext(db_session: Session) -> None:
    """Appendix A: 'retired/deleted C and outer column become NULL in the
    current logical row atomically after stop' — this applies the moment a
    pending object is superseded, not only at #458's later activation.
    blacktomb42 PR #507 review round 1: init_object previously only flipped
    status to 'retired' and left real ciphertext/outer_cipher sitting in a
    row that is already unreachable via any legitimate business path."""
    config_id, revision = _seed_pending_config(db_session)
    first = init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        config_id=config_id,
        request_id=uuid.uuid4(),
        filename="will.pdf",
        plaintext_size=4,
    )
    db_session.flush()
    ready = upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=first.revision,
        object_id=first.object_id,
        config_id=config_id,
        manifest=_manifest(first.vault_id, first.object_id),
        inner_b64=_b64url(b"D" * 32),
        ciphertext=b"C" * 20,
    )
    db_session.flush()
    assert ready.status == "ready"

    init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=ready.revision,
        config_id=config_id,
        request_id=uuid.uuid4(),
        filename="will-v2.pdf",
        plaintext_size=8,
    )
    db_session.flush()

    old = db_session.get(VigilObject, first.object_id)
    assert old is not None
    assert old.status == "retired"
    assert old.ciphertext is None
    assert old.outer_cipher is None


def test_init_stale_revision_on_fresh_request_conflicts(db_session: Session) -> None:
    config_id, revision = _seed_pending_config(db_session)
    with pytest.raises(VigilRevisionConflict) as excinfo:
        init_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision + 99,
            config_id=config_id,
            request_id=uuid.uuid4(),
            filename="will.pdf",
            plaintext_size=1000,
        )
    assert excinfo.value.current_revision == revision


def test_init_unknown_config_id_not_found(db_session: Session) -> None:
    _config_id, revision = _seed_pending_config(db_session)
    with pytest.raises(VigilObjectNotFound):
        init_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            config_id=uuid.uuid4(),
            request_id=uuid.uuid4(),
            filename="will.pdf",
            plaintext_size=1000,
        )


def test_init_rejects_oversized_plaintext(db_session: Session) -> None:
    config_id, revision = _seed_pending_config(db_session)
    with pytest.raises(VigilObjectInputError):
        init_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            config_id=config_id,
            request_id=uuid.uuid4(),
            filename="will.pdf",
            plaintext_size=10_000_001,
        )


def test_init_accepts_zero_byte_plaintext(db_session: Session) -> None:
    config_id, revision = _seed_pending_config(db_session)
    result = init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        config_id=config_id,
        request_id=uuid.uuid4(),
        filename="empty.txt",
        plaintext_size=0,
    )
    assert result.created is True


def test_init_rejects_empty_filename(db_session: Session) -> None:
    config_id, revision = _seed_pending_config(db_session)
    with pytest.raises(VigilObjectInputError):
        init_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            config_id=config_id,
            request_id=uuid.uuid4(),
            filename="   ",
            plaintext_size=10,
        )


def test_init_sanitizes_filename_to_basename(db_session: Session) -> None:
    config_id, revision = _seed_pending_config(db_session)
    result = init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        config_id=config_id,
        request_id=uuid.uuid4(),
        filename="../../etc/passwd",
        plaintext_size=10,
    )
    obj = db_session.get(VigilObject, result.object_id)
    assert obj is not None
    from app.services.vigil.crypto import decrypt_field

    filename = decrypt_field(
        obj.filename_cipher,
        purpose="vigil_object_filename",
        table="vigil_objects",
        row_id=obj.id,
        vault_id=result.vault_id,
    )
    assert filename == "passwd"


# --- upload_object -----------------------------------------------------------


def _manifest(
    vault_id: uuid.UUID, object_id: uuid.UUID, *, has_password: bool = False
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "vault_id": str(vault_id),
        "object_id": str(object_id),
        "has_password": has_password,
        "file_nonce": base64.urlsafe_b64encode(b"0" * 12).rstrip(b"=").decode(),
        "salt": None,
        "kdf": None,
        "inner_nonce": None,
    }
    if has_password:
        base["salt"] = base64.urlsafe_b64encode(b"1" * 16).rstrip(b"=").decode()
        base["inner_nonce"] = base64.urlsafe_b64encode(b"2" * 12).rstrip(b"=").decode()
        base["kdf"] = {
            "name": "argon2id",
            "version": 19,
            "memory_kib": 65536,
            "iterations": 3,
            "parallelism": 1,
            "length": 32,
        }
    return base


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _init_staging_object(
    db_session: Session, *, plaintext_size: int = 4
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, int]:
    config_id, revision = _seed_pending_config(db_session)
    result = init_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        config_id=config_id,
        request_id=uuid.uuid4(),
        filename="will.pdf",
        plaintext_size=plaintext_size,
    )
    db_session.flush()
    return result.vault_id, config_id, result.object_id, result.revision


def test_upload_fresh_stores_ready_object(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    ciphertext = b"C" * 20  # plaintext(4) + tag(16)
    inner = _b64url(b"D" * 32)
    result = upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        object_id=object_id,
        config_id=config_id,
        manifest=_manifest(vault_id, object_id),
        inner_b64=inner,
        ciphertext=ciphertext,
    )
    db_session.flush()
    assert result.created is True
    assert result.status == "ready"
    obj = db_session.get(VigilObject, object_id)
    assert obj is not None
    assert obj.status == "ready"
    assert obj.ciphertext == ciphertext
    assert obj.ciphertext_size == 20
    assert obj.cipher_sha256 == hashlib.sha256(ciphertext).hexdigest()
    assert obj.manifest is not None


def test_upload_zero_byte_file_ciphertext_length_16(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=0)
    ciphertext = b"T" * 16
    inner = _b64url(b"D" * 32)
    upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        object_id=object_id,
        config_id=config_id,
        manifest=_manifest(vault_id, object_id),
        inner_b64=inner,
        ciphertext=ciphertext,
    )
    obj = db_session.get(VigilObject, object_id)
    assert obj is not None
    assert obj.status == "ready"


def test_upload_rejects_ciphertext_length_mismatch(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    inner = _b64url(b"D" * 32)
    with pytest.raises(VigilObjectInputError):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=config_id,
            manifest=_manifest(vault_id, object_id),
            inner_b64=inner,
            ciphertext=b"X" * 19,  # should be 20
        )
    obj = db_session.get(VigilObject, object_id)
    assert obj is not None
    assert obj.status == "staging"


def test_upload_rejects_wrong_inner_length_without_password(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    with pytest.raises(VigilObjectInputError):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=config_id,
            manifest=_manifest(vault_id, object_id),
            inner_b64=_b64url(b"D" * 48),  # wrong: no-password inner must be 32 bytes
            ciphertext=b"C" * 20,
        )


def test_upload_accepts_password_inner_48_bytes(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    result = upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        object_id=object_id,
        config_id=config_id,
        manifest=_manifest(vault_id, object_id, has_password=True),
        inner_b64=_b64url(b"D" * 48),
        ciphertext=b"C" * 20,
    )
    assert result.status == "ready"


def test_upload_rejects_manifest_vault_id_mismatch(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    bad_manifest = _manifest(vault_id, object_id)
    bad_manifest["vault_id"] = str(uuid.uuid4())
    with pytest.raises(VigilObjectInputError):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=config_id,
            manifest=bad_manifest,
            inner_b64=_b64url(b"D" * 32),
            ciphertext=b"C" * 20,
        )


def test_upload_rejects_unknown_manifest_field(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    bad_manifest = _manifest(vault_id, object_id)
    bad_manifest["extra"] = "nope"
    with pytest.raises(VigilObjectInputError):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=config_id,
            manifest=bad_manifest,
            inner_b64=_b64url(b"D" * 32),
            ciphertext=b"C" * 20,
        )


def test_upload_rejects_wrong_kdf_params(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    bad_manifest = _manifest(vault_id, object_id, has_password=True)
    bad_manifest["kdf"]["iterations"] = 1
    with pytest.raises(VigilObjectInputError):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=config_id,
            manifest=bad_manifest,
            inner_b64=_b64url(b"D" * 48),
            ciphertext=b"C" * 20,
        )


def test_upload_rejects_password_field(db_session: Session) -> None:
    """Server never accepts a plaintext/password field at all (#450 Design
    section 5) — passing one through the manifest is rejected as unknown."""
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    bad_manifest = _manifest(vault_id, object_id)
    bad_manifest["password"] = "hunter2"
    with pytest.raises(VigilObjectInputError):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=config_id,
            manifest=bad_manifest,
            inner_b64=_b64url(b"D" * 32),
            ciphertext=b"C" * 20,
        )


def test_upload_exact_ready_replay_returns_without_mutation(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    ciphertext = b"C" * 20
    inner = _b64url(b"D" * 32)
    manifest = _manifest(vault_id, object_id)
    upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        object_id=object_id,
        config_id=config_id,
        manifest=manifest,
        inner_b64=inner,
        ciphertext=ciphertext,
    )
    db_session.flush()

    replay = upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision - 12345,  # deliberately wrong/stale
        object_id=object_id,
        config_id=config_id,
        manifest=manifest,
        inner_b64=inner,
        ciphertext=ciphertext,
    )
    assert replay.created is False
    assert replay.status == "ready"


def test_upload_mismatched_ready_retry_conflicts(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    ciphertext = b"C" * 20
    inner = _b64url(b"D" * 32)
    manifest = _manifest(vault_id, object_id)
    upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        object_id=object_id,
        config_id=config_id,
        manifest=manifest,
        inner_b64=inner,
        ciphertext=ciphertext,
    )
    db_session.flush()

    with pytest.raises(VigilObjectConflict):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=config_id,
            manifest=manifest,
            inner_b64=inner,
            ciphertext=b"D" * 20,  # different content
        )


def test_upload_never_returns_decrypted_inner(db_session: Session) -> None:
    """The replay-comparison decrypt of the stored inner must never leak
    into the returned result object."""
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    ciphertext = b"C" * 20
    inner = _b64url(b"D" * 32)
    manifest = _manifest(vault_id, object_id)
    upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        object_id=object_id,
        config_id=config_id,
        manifest=manifest,
        inner_b64=inner,
        ciphertext=ciphertext,
    )
    db_session.flush()
    replay = upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        object_id=object_id,
        config_id=config_id,
        manifest=manifest,
        inner_b64=inner,
        ciphertext=ciphertext,
    )
    for value in vars(replay).values():
        assert value != inner
        if isinstance(value, bytes):
            assert b"D" * 32 not in value


def test_upload_stale_revision_on_fresh_upload_conflicts(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    with pytest.raises(VigilRevisionConflict):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision + 999,
            object_id=object_id,
            config_id=config_id,
            manifest=_manifest(vault_id, object_id),
            inner_b64=_b64url(b"D" * 32),
            ciphertext=b"C" * 20,
        )


def test_upload_config_id_mismatch_rejected(db_session: Session) -> None:
    vault_id, _config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    with pytest.raises(VigilObjectInputError):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=uuid.uuid4(),
            manifest=_manifest(vault_id, object_id),
            inner_b64=_b64url(b"D" * 32),
            ciphertext=b"C" * 20,
        )


def test_upload_unknown_object_id_not_found(db_session: Session) -> None:
    vault_id, config_id, _object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    fake_object_id = uuid.uuid4()
    with pytest.raises(VigilObjectNotFound):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=fake_object_id,
            config_id=config_id,
            manifest=_manifest(vault_id, fake_object_id),
            inner_b64=_b64url(b"D" * 32),
            ciphertext=b"C" * 20,
        )


def test_upload_failed_transaction_leaves_object_staging(db_session: Session) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(db_session, plaintext_size=4)
    with pytest.raises(VigilObjectInputError):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=config_id,
            manifest=_manifest(vault_id, object_id),
            inner_b64=_b64url(b"D" * 32),
            ciphertext=b"bad-length",
        )
    db_session.flush()
    obj = db_session.get(VigilObject, object_id)
    assert obj is not None
    assert obj.status == "staging"
    assert obj.ciphertext is None


# --- P2.1-A01 (=A03): exact max-size boundary --------------------------------


def test_upload_max_size_plaintext_ciphertext_length_10000016(db_session: Session) -> None:
    """C == 10,000,016 for a 10,000,000-byte plaintext is accepted (the
    other boundary — C == 16 for a zero-byte plaintext — is exercised by
    test_upload_zero_byte_file_ciphertext_length_16)."""
    vault_id, config_id, object_id, revision = _init_staging_object(
        db_session, plaintext_size=10_000_000
    )
    ciphertext = b"C" * 10_000_016
    result = upload_object(
        db_session,
        owner_user_id=_OWNER_ID,
        expected_revision=revision,
        object_id=object_id,
        config_id=config_id,
        manifest=_manifest(vault_id, object_id),
        inner_b64=_b64url(b"D" * 32),
        ciphertext=ciphertext,
    )
    assert result.status == "ready"
    obj = db_session.get(VigilObject, object_id)
    assert obj is not None
    assert obj.ciphertext is not None
    assert len(obj.ciphertext) == 10_000_016


def test_upload_max_size_boundary_plus_one_byte_rejected_no_ready_row(
    db_session: Session,
) -> None:
    vault_id, config_id, object_id, revision = _init_staging_object(
        db_session, plaintext_size=10_000_000
    )
    with pytest.raises(VigilObjectInputError):
        upload_object(
            db_session,
            owner_user_id=_OWNER_ID,
            expected_revision=revision,
            object_id=object_id,
            config_id=config_id,
            manifest=_manifest(vault_id, object_id),
            inner_b64=_b64url(b"D" * 32),
            ciphertext=b"C" * 10_000_017,
        )
    obj = db_session.get(VigilObject, object_id)
    assert obj is not None
    assert obj.status == "staging"
    assert obj.ciphertext is None
