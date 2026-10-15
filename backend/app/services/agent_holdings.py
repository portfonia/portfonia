"""Strict owned-book operations serialized with every browser/file writer."""

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.user import User
from app.schemas.agent_holdings import Create, Patch, ReplaceAll, Validation, fixed
from app.schemas.holdings import VALID_CURRENCIES, validate_cash_wmf
from app.services import agent_idempotency, holdings_write
from app.services.holding_ordering import sorted_holdings
from app.services.subscription import is_jade

logger = logging.getLogger(__name__)
INPUT_FIELDS = (
    "name",
    "currency",
    "pricing_mode",
    "ticker",
    "fund_code",
    "shares",
    "avg_cost",
    "current_value",
    "asset_type",
    "market",
    "broker",
    "account",
    "portfolio",
    "notes",
    "watch_tier",
)
META_FIELDS = (
    "asset_class",
    "capture_supported",
    "last_manual_update",
    "created_at",
    "updated_at",
    "position",
)


def error(status: int, code: str, message: str, **extra: object) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def entitlement(user: User) -> None:
    if not is_jade(user):
        raise error(403, "subscription_required", "An active Jade subscription is required.")


def input_json(row: Holding) -> dict[str, object]:
    return {
        field: fixed(value) if isinstance(value, Decimal) else value
        for field in INPUT_FIELDS
        for value in [getattr(row, field)]
    }


def record(row: Holding) -> dict[str, object]:
    return {"id": str(row.id), **input_json(row)}


def book(user: User, rows: list[Holding]) -> dict[str, object]:
    ordered = sorted_holdings(rows)
    return {
        "version": str(user.holdings_version),
        "holdings": [record(row) for row in ordered],
        "metadata": [
            {
                "id": str(row.id),
                **{
                    field: value.isoformat() if hasattr(value, "isoformat") else value
                    for field in META_FIELDS
                    for value in [getattr(row, field)]
                },
            }
            for row in ordered
        ],
    }


def read_lock(session: Session, uid: UUID) -> tuple[User, list[Holding]]:
    user = session.execute(
        select(User)
        .where(User.id == uid)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one()
    entitlement(user)
    rows = list(session.scalars(select(Holding).where(Holding.user_id == uid)).all())
    return user, rows


def get_book(session: Session, uid: UUID) -> dict[str, object]:
    user, rows = read_lock(session, uid)
    result = book(user, rows)
    session.commit()
    return result


def normalize(data: dict[str, object], path: str) -> dict[str, object]:
    # Input values have been type checked by the strict transport schema.
    if not cast(str, data["name"]).strip():
        raise error(422, "invalid_holding", "Name must be nonempty.", path=f"{path}.name")
    if data["currency"] not in VALID_CURRENCIES:
        raise error(
            422,
            "invalid_holding",
            "Currency must be a supported uppercase code.",
            path=f"{path}.currency",
        )
    try:
        validate_cash_wmf(
            name=cast(str, data["name"]),
            asset_type=cast(str | None, data.get("asset_type")),
            ticker=cast(str | None, data.get("ticker")),
            fund_code=cast(str | None, data.get("fund_code")),
            pricing_mode=cast(str, data["pricing_mode"]),
            current_value=data.get("current_value"),
        )
    except ValueError:
        raise error(
            422,
            "invalid_holding",
            "Cash and WMF require manual current_value and no instrument.",
            path=path,
        ) from None
    return holdings_write.apply_write_defaults(data)


def writable_json(data: dict[str, object]) -> dict[str, object]:
    return {
        field: fixed(value) if isinstance(value, Decimal) else value
        for field in INPUT_FIELDS
        for value in [data.get(field)]
    }


def owned(rows: list[Holding], hid: UUID) -> Holding:
    row = next((row for row in rows if row.id == hid), None)
    if row is None:
        raise error(404, "holding_not_found", "Holding is unavailable.")
    return row


@dataclass
class Plan:
    targets: list[tuple[Holding | None, dict[str, object], int | None]]
    deleted: list[Holding]
    normalized: dict[str, object]
    whole: bool


def plan(
    user: User,
    rows: list[Holding],
    body: Create | ReplaceAll | Patch | None,
    hid: UUID | None,
    version: str,
) -> Plan:
    if version != str(user.holdings_version):
        raise error(
            409,
            "version_conflict",
            "Reread the current holdings before writing.",
            current_version=str(user.holdings_version),
            path="version",
        )
    targets: list[tuple[Holding | None, dict[str, object], int | None]] = []
    deleted: list[Holding] = []
    normalized: dict[str, object] = {"version": version}
    whole = isinstance(body, ReplaceAll)
    if isinstance(body, ReplaceAll):
        if not body.holdings:
            raise error(422, "empty_holdings", "Clear holdings in the browser UI.", path="holdings")
        ids = [row.id for row in body.holdings if row.id is not None]
        if len(set(ids)) != len(ids) or len(set(body.delete_ids)) != len(body.delete_ids):
            raise error(422, "duplicate_id", "Each ID must occur once.")
        if set(ids) & set(body.delete_ids):
            raise error(422, "conflicting_id", "Retained and deleted IDs must be disjoint.")
        for row_id in [*ids, *body.delete_ids]:
            owned(rows, row_id)
        missing = {row.id for row in rows} - set(ids) - set(body.delete_ids)
        if missing:
            raise error(
                422,
                "incomplete_collection",
                "Explicitly retain or delete every owned ID.",
                missing_ids=sorted(str(i) for i in missing),
            )
        for index, candidate in enumerate(body.holdings):
            data = normalize(candidate.model_dump(exclude={"id"}), f"holdings.{index}")
            targets.append((owned(rows, candidate.id) if candidate.id else None, data, index))
        deleted = [owned(rows, i) for i in body.delete_ids]
        normalized.update(
            holdings=[
                {"id": str(candidate.id) if candidate.id else None, **writable_json(data)}
                for candidate, (_, data, _) in zip(body.holdings, targets, strict=True)
            ],
            delete_ids=[str(i) for i in body.delete_ids],
        )
    elif isinstance(body, Create):
        current = owned(rows, hid) if hid is not None else None
        data = normalize(body.holding.model_dump(), "holding")
        position = (
            current.position
            if current
            else max((r.position for r in rows if r.position is not None), default=-1) + 1
        )
        targets.append((current, data, position))
        normalized["holding"] = writable_json(data)
    elif isinstance(body, Patch):
        assert hid is not None
        current = owned(rows, hid)
        data = {field: getattr(current, field) for field in INPUT_FIELDS}
        changes = body.changes.model_dump(exclude_unset=True)
        if changes:
            data.update(changes)
            data = normalize(data, "changes")
        else:
            # Empty PATCH preserves stored fields, including legacy rows.
            data.update(
                asset_class=current.asset_class, capture_supported=current.capture_supported
            )
        targets.append((current, data, current.position))
        normalized["changes"] = writable_json(data) if changes else {}
    else:
        assert hid is not None
        deleted = [owned(rows, hid)]
        if len(rows) == 1:
            raise error(409, "last_holding_requires_ui", "Clear holdings in the browser UI.")
    after = len(rows) - len(deleted) + sum(row is None for row, _, _ in targets)
    try:
        holdings_write._ensure_cap(
            user, len(rows), after, introduces_rows=any(row is None for row, _, _ in targets)
        )
    except HTTPException:
        raise error(
            409,
            "holding_limit",
            "The final book exceeds the plan's row limit.",
            limit=holdings_write.holding_cap(user),
            current_count=len(rows),
        ) from None
    return Plan(targets, deleted, normalized, whole)


def canonical(body: Create) -> dict[str, object]:
    # Only request-local normalization: never consult changed server state.
    data = body.model_dump(mode="json")
    return data


def mutate(
    session: Session,
    uid: UUID,
    permission: str,
    body: Create | ReplaceAll | Patch | None,
    *,
    token_id: UUID,
    hid: UUID | None = None,
    version: str | None = None,
    key: str | None = None,
) -> tuple[int, dict[str, object], bool, bool]:
    user, rows = holdings_write.lock_user_book(session, uid)
    entitlement(user)
    if permission != "holdings_write":
        raise error(403, "holdings_write_required", "Use a token with holdings_write permission.")
    operation = "holding_create" if isinstance(body, Create) and hid is None else None
    hashed_key = agent_idempotency.key_hash(key) if operation else None
    request_hash = (
        agent_idempotency.canonical_hash(canonical(body))
        if isinstance(body, Create) and operation
        else None
    )
    if operation and hashed_key:
        previous = agent_idempotency.lookup(session, uid, operation, hashed_key)
        if previous is not None:
            if previous.request_hash != request_hash:
                raise error(
                    409, "idempotency_conflict", "The key already identifies different content."
                )
            payload = cast(dict[str, object], json.loads(previous.response_json))
            session.commit()
            return previous.response_status, payload, True, False
    submitted = body.version if body is not None else version
    if submitted is None:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("body", "version"), "msg": "Field required", "input": {}}]
        )
    try:
        prepared = plan(user, rows, body, hid, submitted)
        saved, capture, changed = holdings_write.apply_agent_book(
            session,
            user,
            prepared.targets,
            prepared.deleted,
            whole=prepared.whole,
            token_id=token_id,
        )
        if isinstance(body, ReplaceAll):
            payload = book(user, saved)
        elif body is None:
            payload = {"version": str(user.holdings_version), "deleted_id": str(hid)}
        else:
            payload = {"version": str(user.holdings_version), "holding": record(saved[0])}
        status = 201 if operation == "holding_create" else 200
        if operation and hashed_key and request_hash:
            agent_idempotency.save(
                session, uid, operation, hashed_key, request_hash, status, payload
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    if capture:
        try:
            holdings_write.enqueue_agent_capture(session, uid, capture)
        except Exception:
            logger.error("Agent capture enqueue failed user_id=%s", uid)
    return status, payload, False, changed


def validate(session: Session, uid: UUID, body: Validation) -> dict[str, object]:
    user, rows = read_lock(session, uid)
    version = str(user.holdings_version)
    hid = getattr(body, "holding_id", None)
    try:
        prepared = plan(user, rows, body, hid, body.version)
        normalized = {"operation": body.operation, **prepared.normalized}
        if hid is not None:
            normalized["holding_id"] = str(hid)
        result = {"valid": True, "version": version, "normalized": normalized}
    except HTTPException as exc:
        detail = exc.detail
        if not isinstance(detail, dict):
            raise
        result = {
            "valid": False,
            "version": version,
            "errors": [
                {
                    "code": detail["code"],
                    "path": detail.get("path", ""),
                    "message": detail["message"],
                }
            ],
        }
    session.commit()
    return result
