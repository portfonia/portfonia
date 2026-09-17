"""POST /vigil/configurations business logic (issue #454, Vigil R0 P2.1).

Split into three phases the router composes (Design section 5: "DNS checks
occur outside locks; check off-lock then revalidate submitted normalized
values on commit"):

1. `validate_configuration_input` — pure, no DB/network: interval/grace/
   recipient-count/dedupe/confirm-match/message-length bounds, email
   normalization. Raises `VigilConfigurationInputError` (-> 422).
2. `check_recipients_dns` — network, no lock held. Raises
   `VigilConfigurationInputError` (-> 422, no valid mail route) or
   `VigilDnsUnavailable` (-> 503, transient failure, no state change).
3. `write_pending_configuration` — DB, under the User-then-vault lock order
   (#450 Design section 3). Raises `VigilRevisionConflict` (-> 409).

`data_cipher`'s business JSON shape is Appendix A's
{interval_days,grace_hours,account_email,recipients:[{position,email}],
message} — encrypted as one field via services.vigil.crypto, not scattered
across separate DB columns (no separate searchable recipient table, per
Appendix A "no separate recipient table or searchable email hash").
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.vigil import VigilConfiguration, VigilVault
from app.services.vigil.crypto import decrypt_field, encrypt_field

VALID_INTERVAL_DAYS = (7, 14, 30, 60, 90)
DEFAULT_INTERVAL_DAYS = 30
MIN_GRACE_HOURS = 24
MAX_GRACE_HOURS = 168
DEFAULT_GRACE_HOURS = 72
MIN_RECIPIENTS = 1
MAX_RECIPIENTS = 3
MAX_MESSAGE_CODEPOINTS = 4000

_CONFIGURATION_PURPOSE = "vigil_configuration"


class VigilConfigurationInputError(ValueError):
    """Malformed/out-of-bounds request data (-> 422). Never carries the
    raw recipient list in its message — only the reason."""


class VigilRevisionConflict(RuntimeError):
    """expected_revision didn't match the vault's current revision (-> 409,
    no mutation). Carries the real current revision so the caller can retry."""

    def __init__(self, current_revision: int) -> None:
        super().__init__("vigil vault revision conflict")
        self.current_revision = current_revision


class VigilRecipientsLocked(RuntimeError):
    """Recipients/order cannot change once a vault has been armed at least
    once, even after a later disarm (#450 Design section 5) (-> 422)."""


def normalize_email(raw: str) -> str:
    """Trim enclosing whitespace, preserve local-part case, lowercase +
    IDNA-encode the domain (#450 Design section 5)."""
    stripped = raw.strip()
    local, sep, domain = stripped.rpartition("@")
    if not sep or not local or not domain:
        raise ValueError(f"not a valid email address: {raw!r}")
    try:
        domain_norm = domain.strip().lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid email domain: {domain!r}") from exc
    if not domain_norm:
        raise ValueError(f"invalid email domain: {domain!r}")
    return f"{local}@{domain_norm}"


@dataclass(frozen=True)
class NormalizedRecipient:
    position: int
    email: str


@dataclass(frozen=True)
class NormalizedConfiguration:
    interval_days: int
    grace_hours: int
    recipients: tuple[NormalizedRecipient, ...]
    message: str


def validate_configuration_input(
    *,
    interval_days: int | None,
    grace_hours: int | None,
    recipients: list[dict[str, str]],
    message: str | None,
) -> NormalizedConfiguration:
    resolved_interval = DEFAULT_INTERVAL_DAYS if interval_days is None else interval_days
    if resolved_interval not in VALID_INTERVAL_DAYS:
        raise VigilConfigurationInputError(f"interval_days must be one of {VALID_INTERVAL_DAYS}")

    resolved_grace = DEFAULT_GRACE_HOURS if grace_hours is None else grace_hours
    if not (MIN_GRACE_HOURS <= resolved_grace <= MAX_GRACE_HOURS):
        raise VigilConfigurationInputError(
            f"grace_hours must be between {MIN_GRACE_HOURS} and {MAX_GRACE_HOURS}"
        )

    if not (MIN_RECIPIENTS <= len(recipients) <= MAX_RECIPIENTS):
        raise VigilConfigurationInputError(
            f"recipients must have between {MIN_RECIPIENTS} and {MAX_RECIPIENTS} entries"
        )

    normalized_recipients: list[NormalizedRecipient] = []
    seen: set[str] = set()
    for position, entry in enumerate(recipients, start=1):
        raw_email = entry.get("email", "")
        raw_confirm = entry.get("email_confirm", "")
        try:
            email = normalize_email(raw_email)
            confirm = normalize_email(raw_confirm)
        except ValueError as exc:
            raise VigilConfigurationInputError(str(exc)) from exc
        if email != confirm:
            raise VigilConfigurationInputError("email and email_confirm must match")
        key = email.lower()
        if key in seen:
            raise VigilConfigurationInputError("recipients must not contain duplicate addresses")
        seen.add(key)
        normalized_recipients.append(NormalizedRecipient(position=position, email=email))

    resolved_message = "" if message is None else message
    if len(resolved_message) > MAX_MESSAGE_CODEPOINTS:
        raise VigilConfigurationInputError(
            f"message must be at most {MAX_MESSAGE_CODEPOINTS} codepoints"
        )

    return NormalizedConfiguration(
        interval_days=resolved_interval,
        grace_hours=resolved_grace,
        recipients=tuple(normalized_recipients),
        message=resolved_message,
    )


def _decrypt_configuration_data(config: VigilConfiguration, vault_id: UUID) -> dict[str, object]:
    plaintext = decrypt_field(
        config.data_cipher,
        purpose=_CONFIGURATION_PURPOSE,
        table="vigil_configurations",
        row_id=config.id,
        vault_id=vault_id,
    )
    data: dict[str, object] = json.loads(plaintext)
    return data


def _assert_recipients_not_changed_after_arming(
    session: Session, vault: VigilVault, normalized: NormalizedConfiguration
) -> None:
    """Once armed at least once, recipients/order are fixed forever — even
    after a later disarm (#450 Design section 5)."""
    if vault.first_armed_at is None:
        return
    reference_id = vault.active_config_id or vault.pending_config_id
    if reference_id is None:
        return
    reference = session.get(VigilConfiguration, reference_id)
    if reference is None:
        return
    data = _decrypt_configuration_data(reference, vault.id)
    existing_recipients = data.get("recipients", [])
    submitted = [{"position": r.position, "email": r.email} for r in normalized.recipients]
    if existing_recipients != submitted:
        raise VigilRecipientsLocked(
            "recipients cannot change after the vault has been armed at least once"
        )


@dataclass(frozen=True)
class ConfigurationWriteResult:
    vault_id: UUID
    config_id: UUID
    revision: int


def write_pending_configuration(
    session: Session,
    *,
    owner_user_id: UUID,
    owner_email: str,
    expected_revision: int,
    normalized: NormalizedConfiguration,
) -> ConfigurationWriteResult:
    """Persist `normalized` as a new pending configuration under the
    User-then-vault lock order (#450 Design section 3). Creates the vault
    on this user's first successful write (expected_revision must be 0 in
    that case, matching GET /vigil/vault's absent-vault contract)."""
    session.execute(select(User).where(User.id == owner_user_id).with_for_update())

    vault = session.scalars(
        select(VigilVault).where(VigilVault.owner_user_id == owner_user_id).with_for_update()
    ).one_or_none()

    if vault is None:
        if expected_revision != 0:
            raise VigilRevisionConflict(current_revision=0)
        vault = VigilVault(owner_user_id=owner_user_id)
        session.add(vault)
        session.flush()
    else:
        if expected_revision != vault.revision:
            raise VigilRevisionConflict(current_revision=vault.revision)

    _assert_recipients_not_changed_after_arming(session, vault, normalized)

    next_config_revision = (
        session.scalar(
            select(func.max(VigilConfiguration.config_revision)).where(
                VigilConfiguration.vault_id == vault.id
            )
        )
        or 0
    ) + 1

    if vault.pending_config_id is not None:
        pending = session.get(VigilConfiguration, vault.pending_config_id)
        if pending is not None and pending.status == "pending":
            pending.status = "retired"

    business_data = {
        "interval_days": normalized.interval_days,
        "grace_hours": normalized.grace_hours,
        "account_email": owner_email,
        "recipients": [{"position": r.position, "email": r.email} for r in normalized.recipients],
        "message": normalized.message,
    }
    config_id = uuid.uuid4()
    data_cipher = encrypt_field(
        json.dumps(business_data, separators=(",", ":"), sort_keys=True),
        purpose=_CONFIGURATION_PURPOSE,
        table="vigil_configurations",
        row_id=config_id,
        vault_id=vault.id,
    )
    new_config = VigilConfiguration(
        id=config_id,
        vault_id=vault.id,
        config_revision=next_config_revision,
        status="pending",
        data_cipher=data_cipher,
    )
    session.add(new_config)
    session.flush()

    vault.pending_config_id = new_config.id
    vault.revision += 1
    session.flush()

    return ConfigurationWriteResult(
        vault_id=vault.id, config_id=new_config.id, revision=vault.revision
    )
