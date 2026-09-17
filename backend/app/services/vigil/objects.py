"""POST /vigil/objects/init and POST /vigil/objects/upload business logic
(issue #454, Vigil R0 P2.1).

Idempotency precedes the optimistic-revision lock (both endpoints): a
request whose (vault_id, request_id) — init — or object content — upload —
exactly matches a prior successful call is a replay and returns 200
regardless of whether the caller's `expected_revision` has since gone
stale, since nothing about vault state changes on a replay. Only a
genuinely new write is gated by `expected_revision == vault.revision`
(#450 Design section 3's optimistic-concurrency contract). A request whose
content differs from the stored row it collides with is a real conflict
(409) independent of revision, too.

`services/vigil/configuration.py`'s `VigilRevisionConflict` is reused here
rather than redefined — both endpoints share the exact same vault-scoped
optimistic-concurrency semantics.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import (
    VIGIL_OBJECT_GCM_TAG_LENGTH,
    VIGIL_OBJECT_MAX_PLAINTEXT_SIZE,
    VigilObject,
    VigilVault,
)
from app.services.vigil.configuration import VigilRevisionConflict
from app.services.vigil.crypto import decrypt_field, encrypt_field

MAX_FILENAME_CODEPOINTS = 255
_FILENAME_PURPOSE = "vigil_object_filename"
_OUTER_PURPOSE = "vigil_object_outer"
_MANIFEST_ALGORITHM = "AES-256-GCM"
_MANIFEST_VERSION = 1
_INNER_NO_PASSWORD_LENGTH = 32
_INNER_WITH_PASSWORD_LENGTH = 48
_MAX_UPLOAD_BODY_BYTES = 10_100_000
_REQUIRED_KDF = {
    "name": "argon2id",
    "version": 19,
    "memory_kib": 65536,
    "iterations": 3,
    "parallelism": 1,
    "length": 32,
}
_MANIFEST_FIELDS = frozenset(
    {
        "version",
        "algorithm",
        "vault_id",
        "object_id",
        "has_password",
        "file_nonce",
        "salt",
        "kdf",
        "inner_nonce",
    }
)


class VigilObjectInputError(ValueError):
    """Malformed/out-of-bounds request data (-> 422)."""


class VigilObjectConflict(RuntimeError):
    """A request_id/object replay whose content differs from the row it
    collides with, or a request targeting an object that's been superseded
    (-> 409, no mutation)."""


class VigilObjectNotFound(RuntimeError):
    """object_id/config_id doesn't belong to the caller's own vault
    (-> 404)."""


__all__ = [
    "VigilObjectConflict",
    "VigilObjectInputError",
    "VigilObjectNotFound",
    "VigilRevisionConflict",
    "init_object",
    "upload_object",
]


def _sanitize_filename(raw: str) -> str:
    name = posixpath.basename(raw.strip())
    name = "".join(ch for ch in name if ch.isprintable())
    if not name or len(name) > MAX_FILENAME_CODEPOINTS:
        raise VigilObjectInputError("invalid filename")
    return name


def _decrypt_filename(obj: VigilObject, vault_id: UUID) -> str:
    return decrypt_field(
        obj.filename_cipher,
        purpose=_FILENAME_PURPOSE,
        table="vigil_objects",
        row_id=obj.id,
        vault_id=vault_id,
    )


def _lock_user_and_vault(session: Session, owner_user_id: UUID) -> VigilVault | None:
    session.execute(select(User).where(User.id == owner_user_id).with_for_update())
    return session.scalars(
        select(VigilVault).where(VigilVault.owner_user_id == owner_user_id).with_for_update()
    ).one_or_none()


@dataclass(frozen=True)
class ObjectInitResult:
    vault_id: UUID
    object_id: UUID
    revision: int
    created: bool


def init_object(
    session: Session,
    *,
    owner_user_id: UUID,
    expected_revision: int,
    config_id: UUID,
    request_id: UUID,
    filename: str,
    plaintext_size: int,
) -> ObjectInitResult:
    vault = _lock_user_and_vault(session, owner_user_id)
    if vault is None:
        raise VigilObjectNotFound("no vault for this owner")
    if config_id not in (vault.active_config_id, vault.pending_config_id):
        raise VigilObjectNotFound("config_id does not belong to this vault")
    if not (0 <= plaintext_size <= VIGIL_OBJECT_MAX_PLAINTEXT_SIZE):
        raise VigilObjectInputError(
            f"plaintext_size must be between 0 and {VIGIL_OBJECT_MAX_PLAINTEXT_SIZE}"
        )
    clean_filename = _sanitize_filename(filename)

    existing = session.scalars(
        select(VigilObject).where(
            VigilObject.vault_id == vault.id, VigilObject.request_id == request_id
        )
    ).one_or_none()
    if existing is not None:
        is_current = existing.id in (vault.pending_object_id, vault.active_object_id)
        same_input = (
            existing.config_id == config_id
            and existing.plaintext_size == plaintext_size
            and _decrypt_filename(existing, vault.id) == clean_filename
        )
        if is_current and same_input:
            return ObjectInitResult(
                vault_id=vault.id, object_id=existing.id, revision=vault.revision, created=False
            )
        raise VigilObjectConflict(
            "request_id already used with different input, or its object was superseded"
        )

    if expected_revision != vault.revision:
        raise VigilRevisionConflict(current_revision=vault.revision)

    # Retire only the previous PENDING object (staging or ready) — never active.
    if vault.pending_object_id is not None:
        old_pending = session.get(VigilObject, vault.pending_object_id)
        if old_pending is not None and old_pending.status in ("staging", "ready"):
            old_pending.status = "retired"

    new_id = uuid.uuid4()
    obj = VigilObject(
        id=new_id,
        vault_id=vault.id,
        config_id=config_id,
        request_id=request_id,
        status="staging",
        filename_cipher=encrypt_field(
            clean_filename,
            purpose=_FILENAME_PURPOSE,
            table="vigil_objects",
            row_id=new_id,
            vault_id=vault.id,
        ),
        plaintext_size=plaintext_size,
    )
    session.add(obj)
    session.flush()

    vault.pending_object_id = obj.id
    vault.revision += 1
    session.flush()

    return ObjectInitResult(
        vault_id=vault.id, object_id=obj.id, revision=vault.revision, created=True
    )


def _b64url_decode(value: object, *, field: str) -> bytes:
    if not isinstance(value, str):
        raise VigilObjectInputError(f"{field} must be a base64url string")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError) as exc:
        raise VigilObjectInputError(f"{field} is not valid base64url") from exc


def _validate_manifest(manifest: dict[str, Any], *, vault_id: UUID, object_id: UUID) -> bool:
    if not isinstance(manifest, dict):
        raise VigilObjectInputError("manifest must be a JSON object")
    unknown = set(manifest) - _MANIFEST_FIELDS
    if unknown:
        raise VigilObjectInputError(f"manifest has unrecognized field(s): {sorted(unknown)}")
    missing = _MANIFEST_FIELDS - set(manifest)
    if missing:
        raise VigilObjectInputError(f"manifest is missing field(s): {sorted(missing)}")

    if manifest["version"] != _MANIFEST_VERSION:
        raise VigilObjectInputError("unsupported manifest version")
    if manifest["algorithm"] != _MANIFEST_ALGORITHM:
        raise VigilObjectInputError("unsupported manifest algorithm")
    if manifest["vault_id"] != str(vault_id):
        raise VigilObjectInputError("manifest vault_id does not match")
    if manifest["object_id"] != str(object_id):
        raise VigilObjectInputError("manifest object_id does not match")

    has_password = manifest["has_password"]
    if not isinstance(has_password, bool):
        raise VigilObjectInputError("manifest has_password must be a boolean")

    file_nonce = _b64url_decode(manifest["file_nonce"], field="file_nonce")
    if len(file_nonce) != 12:
        raise VigilObjectInputError("file_nonce must decode to 12 bytes")

    if has_password:
        salt = _b64url_decode(manifest["salt"], field="salt")
        if len(salt) != 16:
            raise VigilObjectInputError("salt must decode to 16 bytes")
        inner_nonce = _b64url_decode(manifest["inner_nonce"], field="inner_nonce")
        if len(inner_nonce) != 12:
            raise VigilObjectInputError("inner_nonce must decode to 12 bytes")
        if manifest["kdf"] != _REQUIRED_KDF:
            raise VigilObjectInputError("kdf parameters do not match the required contract")
    else:
        if (
            manifest["salt"] is not None
            or manifest["kdf"] is not None
            or manifest["inner_nonce"] is not None
        ):
            raise VigilObjectInputError(
                "salt/kdf/inner_nonce must be null when has_password is false"
            )

    return has_password


@dataclass(frozen=True)
class ObjectUploadResult:
    vault_id: UUID
    object_id: UUID
    status: str
    revision: int
    created: bool


def upload_object(
    session: Session,
    *,
    owner_user_id: UUID,
    expected_revision: int,
    object_id: UUID,
    config_id: UUID,
    manifest: dict[str, Any],
    inner_b64: str,
    ciphertext: bytes,
) -> ObjectUploadResult:
    if len(ciphertext) + len(inner_b64) > _MAX_UPLOAD_BODY_BYTES:
        raise VigilObjectInputError("upload body exceeds the maximum bound")

    vault = _lock_user_and_vault(session, owner_user_id)
    if vault is None:
        raise VigilObjectNotFound("no vault for this owner")

    obj = session.get(VigilObject, object_id)
    if obj is None or obj.vault_id != vault.id:
        raise VigilObjectNotFound("object_id does not belong to this vault")
    if obj.config_id != config_id:
        raise VigilObjectInputError("config_id does not match this object")

    if obj.status == "ready":
        same_manifest = obj.manifest == manifest
        same_hash = obj.cipher_sha256 == hashlib.sha256(ciphertext).hexdigest()
        same_inner = False
        if same_manifest and same_hash and obj.outer_cipher is not None:
            existing_inner_b64 = decrypt_field(
                obj.outer_cipher,
                purpose=_OUTER_PURPOSE,
                table="vigil_objects",
                row_id=obj.id,
                vault_id=vault.id,
            )
            same_inner = existing_inner_b64 == inner_b64
        if same_manifest and same_hash and same_inner:
            return ObjectUploadResult(
                vault_id=vault.id,
                object_id=obj.id,
                status="ready",
                revision=vault.revision,
                created=False,
            )
        raise VigilObjectConflict("object already has different ready content")

    if obj.status != "staging":
        raise VigilObjectConflict(f"object is not pending upload (status={obj.status!r})")

    if expected_revision != vault.revision:
        raise VigilRevisionConflict(current_revision=vault.revision)

    has_password = _validate_manifest(manifest, vault_id=vault.id, object_id=obj.id)
    inner_bytes = _b64url_decode(inner_b64, field="inner")
    expected_inner_length = (
        _INNER_WITH_PASSWORD_LENGTH if has_password else _INNER_NO_PASSWORD_LENGTH
    )
    if len(inner_bytes) != expected_inner_length:
        raise VigilObjectInputError(
            f"inner must decode to {expected_inner_length} bytes for has_password={has_password}"
        )

    expected_ciphertext_length = obj.plaintext_size + VIGIL_OBJECT_GCM_TAG_LENGTH
    if len(ciphertext) != expected_ciphertext_length:
        raise VigilObjectInputError(
            f"ciphertext must be exactly {expected_ciphertext_length} bytes "
            f"(plaintext_size {obj.plaintext_size} + {VIGIL_OBJECT_GCM_TAG_LENGTH})"
        )

    outer_cipher = encrypt_field(
        inner_b64,
        purpose=_OUTER_PURPOSE,
        table="vigil_objects",
        row_id=obj.id,
        vault_id=vault.id,
    )
    obj.ciphertext = ciphertext
    obj.ciphertext_size = len(ciphertext)
    obj.cipher_sha256 = hashlib.sha256(ciphertext).hexdigest()
    obj.manifest = manifest
    obj.outer_cipher = outer_cipher
    obj.status = "ready"
    session.flush()

    return ObjectUploadResult(
        vault_id=vault.id, object_id=obj.id, status="ready", revision=vault.revision, created=True
    )
