"""Snapshot outbox payload codec + row lifecycle (issue #373, Scenario 2).

The payload is a versioned JSON document whose non-JSON-native scalars
(`Decimal`, `date`, `UUID`) are tagged inline, so decoding reproduces the
frozen row dicts exactly — value *and* type — without a per-column coercion
table that a future schema change could silently invalidate. `None`, `str`,
`bool` and `int` pass through untagged.

A sha256 over the plaintext JSON is stored alongside; `decode_payload`
refuses a payload whose checksum doesn't match. Fernet already authenticates
the ciphertext, so this is a second, format-level guard against a payload
that was edited or re-encoded outside the app.

See `app/services/portfolio_history.py` for who writes these rows and
`app/services/snapshot_recovery.py` for who replays them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.portfolio_snapshot_outbox import PortfolioSnapshotOutbox

logger = logging.getLogger(__name__)

PAYLOAD_VERSION = 1

# Applied rows are redundant once the live rows exist (that IS the durable
# copy) and Postgres backups cover PITR; `computed`/`failed` rows are pending
# recovery evidence and are never pruned. Retention is deliberately longer
# than any plausible "we noticed three weeks later" investigation.
OUTBOX_APPLIED_RETENTION_DAYS = 90

_TAG_DECIMAL = "decimal"
_TAG_DATE = "date"
_TAG_UUID = "uuid"

JsonValue = str | bool | int | None | dict[str, str]
RowDict = dict[str, object]


class OutboxPayloadError(RuntimeError):
    """The stored payload can't be trusted: bad JSON, wrong shape, a checksum
    mismatch, or a value that doesn't decode.

    The message never carries payload content — a payload holds
    holdings-derived values, not loggable material.
    """


def _encode_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return {"t": _TAG_DECIMAL, "v": str(value)}
    if isinstance(value, date):
        return {"t": _TAG_DATE, "v": value.isoformat()}
    if isinstance(value, uuid.UUID):
        return {"t": _TAG_UUID, "v": str(value)}
    raise OutboxPayloadError(f"unsupported snapshot value type: {type(value).__name__}")


def _decode_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, dict):
        tag = value.get("t")
        raw = value.get("v")
        if not isinstance(raw, str):
            raise OutboxPayloadError("tagged payload value is missing its string value")
        if tag == _TAG_DECIMAL:
            try:
                return Decimal(raw)
            except InvalidOperation as exc:
                raise OutboxPayloadError("tagged payload value is not a valid decimal") from exc
        if tag == _TAG_DATE:
            try:
                return date.fromisoformat(raw)
            except ValueError as exc:
                raise OutboxPayloadError("tagged payload value is not a valid date") from exc
        if tag == _TAG_UUID:
            try:
                return uuid.UUID(raw)
            except ValueError as exc:
                raise OutboxPayloadError("tagged payload value is not a valid uuid") from exc
        raise OutboxPayloadError("tagged payload value has an unknown type tag")
    raise OutboxPayloadError(f"payload value has an unsupported JSON type: {type(value).__name__}")


def encode_payload(rows: list[RowDict]) -> tuple[str, str]:
    """Serialize frozen row dicts to (encrypted-at-rest payload text, checksum).

    Deterministic (`sort_keys`) so re-encoding the same rows yields the same
    checksum.
    """
    document: dict[str, Any] = {
        "v": PAYLOAD_VERSION,
        "rows": [{k: _encode_value(v) for k, v in row.items()} for row in rows],
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return payload, _checksum(payload)


def decode_payload(payload: str, checksum: str) -> list[RowDict]:
    if not hmac.compare_digest(_checksum(payload), checksum):
        raise OutboxPayloadError("snapshot outbox payload checksum mismatch")
    try:
        document = json.loads(payload)
    except ValueError as exc:
        raise OutboxPayloadError("snapshot outbox payload is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("v") != PAYLOAD_VERSION:
        raise OutboxPayloadError("snapshot outbox payload version/shape is not recognized")
    raw_rows = document.get("rows")
    if not isinstance(raw_rows, list):
        raise OutboxPayloadError("snapshot outbox payload rows are not a list")
    rows: list[RowDict] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            raise OutboxPayloadError("snapshot outbox payload row is not an object")
        rows.append({str(key): _decode_value(value) for key, value in raw_row.items()})
    return rows


def _checksum(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def get_outbox_row(
    session: Session, user_id: uuid.UUID, snapshot_date: date
) -> PortfolioSnapshotOutbox | None:
    return session.execute(
        select(PortfolioSnapshotOutbox).where(
            PortfolioSnapshotOutbox.user_id == user_id,
            PortfolioSnapshotOutbox.snapshot_date == snapshot_date,
        )
    ).scalar_one_or_none()


def upsert_computed_outbox_row(
    session: Session,
    user_id: uuid.UUID,
    snapshot_date: date,
    rows: list[RowDict],
) -> PortfolioSnapshotOutbox:
    """Freeze `rows` as this (user, day)'s intended payload.

    A same-day re-run deliberately overwrites the previous payload: the daily
    path is expected to publish the latest data for the day it is capturing
    (`_upsert_rows`'s same intent on the live table), and a *late* recovery of
    an older day goes through `snapshot_recovery`, which replays whatever is
    frozen here instead.
    """
    payload, checksum = encode_payload(rows)
    row = get_outbox_row(session, user_id, snapshot_date)
    if row is None:
        row = PortfolioSnapshotOutbox(
            user_id=user_id,
            snapshot_date=snapshot_date,
            status="computed",
            payload=payload,
            checksum=checksum,
        )
        session.add(row)
    else:
        row.payload = payload
        row.checksum = checksum
        row.status = "computed"
        row.applied_at = None
        row.computed_at = func.now()
    session.flush()
    return row


def mark_outbox_applied(row: PortfolioSnapshotOutbox) -> None:
    row.status = "applied"
    row.applied_at = func.now()


def mark_outbox_failed(row: PortfolioSnapshotOutbox) -> None:
    """A payload that can no longer be decoded (checksum/format/decrypt).

    Recovery must not retry such a row forever, but it must stay visible.
    """
    row.status = "failed"


def prune_applied_outbox(
    session: Session, *, today: date, retention_days: int = OUTBOX_APPLIED_RETENTION_DAYS
) -> int:
    """Drop `applied` rows past the retention window; see module docstring.

    Bounded by `snapshot_date`, so a daily call deletes one day's worth.
    """
    cutoff = today - timedelta(days=retention_days)
    result = cast(
        CursorResult[Any],
        session.execute(
            delete(PortfolioSnapshotOutbox).where(
                PortfolioSnapshotOutbox.status == "applied",
                PortfolioSnapshotOutbox.snapshot_date < cutoff,
            )
        ),
    )
    return result.rowcount or 0
