"""Record encrypted field diffs in the holdings write transaction."""

import json
from collections.abc import Sequence
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timezones import ET, today_et
from app.models.api_token import ApiToken
from app.models.holding import Holding
from app.models.holding_change_group import HoldingChangeGroup
from app.models.user import User
from app.schemas.agent_holdings import fixed
from app.schemas.holding_changes import (
    ChangedRow,
    ChangeToken,
    HoldingChangeOut,
    HoldingChangesOut,
    HoldingRollbackPreview,
    WebHoldingChangeOut,
    WebHoldingChangesOut,
)

RECORDED_FIELDS = (
    "name",
    "ticker",
    "fund_code",
    "currency",
    "shares",
    "avg_cost",
    "current_value",
    "pricing_mode",
    "asset_type",
    "market",
    "broker",
    "account",
    "portfolio",
    "notes",
    "watch_tier",
    "position",
)
type Snapshot = dict[UUID, dict[str, object]]


def snapshot(rows: Sequence[Holding]) -> Snapshot:
    return {
        row.id: {
            field: fixed(value) if isinstance(value, Decimal) else value
            for field in RECORDED_FIELDS
            for value in [getattr(row, field)]
        }
        for row in rows
    }


def _position(values: dict[str, object]) -> tuple[bool, int]:
    position = cast(int | None, values["position"])
    return position is None, position if position is not None else 0


def record(
    session: Session,
    user_id: UUID,
    before: Snapshot,
    *,
    source: Literal["web", "file", "agent", "rollback"],
    rollback_to: datetime | None = None,
    token_id: UUID | None = None,
) -> UUID | None:
    session.flush()
    after = snapshot(session.scalars(select(Holding).where(Holding.user_id == user_id)).all())
    entries: list[dict[str, object]] = []
    for hid in [
        *sorted(after, key=lambda i: _position(after[i])),
        *sorted(before.keys() - after.keys(), key=lambda i: _position(before[i])),
    ]:
        old, new = before.get(hid, {}), after.get(hid, {})
        fields = [
            {"field": field, "before": old.get(field), "after": new.get(field)}
            for field in RECORDED_FIELDS
            if old.get(field) != new.get(field)
        ]
        if fields:
            entries.append(
                {
                    "holding_id": str(hid),
                    "holding_name": (new or old)["name"],
                    "kind": "updated" if old and new else "created" if new else "deleted",
                    "fields": fields,
                }
            )
    if not entries:
        return None
    token = session.get(ApiToken, token_id) if source == "agent" and token_id is not None else None
    group = HoldingChangeGroup(
        user_id=user_id,
        source=source,
        rollback_to=rollback_to,
        created_at=func.clock_timestamp(),
        token_name=token.name if token else None,
        token_prefix=token.token_prefix if token else None,
        changes=json.dumps(entries),
    )
    session.add(group)
    session.flush()
    return group.id


def list_changes(session: Session, user_id: UUID, start: date, end: date) -> HoldingChangesOut:
    today = today_et()
    if not today - timedelta(days=29) <= start <= end <= today:
        raise HTTPException(422, "Require today ET minus 29 days <= start <= end <= today ET.")
    rows = session.scalars(
        select(HoldingChangeGroup)
        .where(
            HoldingChangeGroup.user_id == user_id,
            HoldingChangeGroup.created_at >= datetime.combine(start, time.min, ET),
            HoldingChangeGroup.created_at < datetime.combine(end + timedelta(days=1), time.min, ET),
        )
        .order_by(HoldingChangeGroup.created_at.desc(), HoldingChangeGroup.id.desc())
        .limit(101)
    ).all()
    return HoldingChangesOut(
        start=start,
        end=end,
        truncated=len(rows) > 100,
        items=[
            HoldingChangeOut(
                id=row.id,
                changed_at=row.created_at.astimezone(ET),
                source=cast(Literal["web", "file", "agent", "rollback"], row.source),
                rollback_to=row.rollback_to.astimezone(ET) if row.rollback_to else None,
                token=ChangeToken(
                    name=cast(str, row.token_name), prefix=cast(str, row.token_prefix)
                )
                if row.source == "agent"
                else None,
                rows=[ChangedRow.model_validate(value) for value in json.loads(row.changes)],
            )
            for row in rows[:100]
        ],
    )


def retained_groups(session: Session, user_id: UUID) -> list[HoldingChangeGroup]:
    return list(
        session.scalars(
            select(HoldingChangeGroup)
            .where(HoldingChangeGroup.user_id == user_id)
            .order_by(HoldingChangeGroup.created_at.desc(), HoldingChangeGroup.id.desc())
        ).all()
    )


def replay(current: Snapshot, groups: list[HoldingChangeGroup]) -> list[Snapshot | None]:
    state = deepcopy(current)
    results: list[Snapshot | None] = []
    blocked = False
    for group in groups:
        if not blocked:
            for value in json.loads(group.changes):
                entry = ChangedRow.model_validate(value)
                hid = entry.holding_id
                fields = {field.field: field for field in entry.fields}
                if entry.kind == "created":
                    expected = {
                        field: fields[field].after if field in fields else None
                        for field in RECORDED_FIELDS
                    }
                    if state.get(hid) != expected:
                        blocked = True
                        break
                    del state[hid]
                elif entry.kind == "updated":
                    if hid not in state or any(
                        state[hid][field.field] != field.after for field in entry.fields
                    ):
                        blocked = True
                        break
                    for field in entry.fields:
                        state[hid][field.field] = field.before
                else:
                    if hid in state:
                        blocked = True
                        break
                    state[hid] = {
                        field: fields[field].before if field in fields else None
                        for field in RECORDED_FIELDS
                    }
        results.append(None if blocked else deepcopy(state))
    return results


def rollback_target(
    current: Snapshot, groups: list[HoldingChangeGroup], group_id: UUID
) -> tuple[HoldingChangeGroup, Snapshot, int]:
    index = next((i for i, group in enumerate(groups) if group.id == group_id), None)
    if index is None:
        raise HTTPException(404, "Holding change not found.")
    target = replay(current, groups)[index]
    if target is None:
        raise HTTPException(409, "rollback_blocked")
    return groups[index], target, index + 1


def list_web_changes(
    session: Session, user_id: UUID, start: date, end: date
) -> WebHoldingChangesOut:
    result = list_changes(session, user_id, start, end)
    current = snapshot(session.scalars(select(Holding).where(Holding.user_id == user_id)).all())
    groups = retained_groups(session, user_id)
    available = {
        group.id: state is not None
        for group, state in zip(groups, replay(current, groups), strict=True)
    }
    return WebHoldingChangesOut(
        start=result.start,
        end=result.end,
        truncated=result.truncated,
        items=[
            WebHoldingChangeOut(**item.model_dump(), rollback_available=available[item.id])
            for item in result.items
        ],
    )


def preview_rollback(session: Session, user_id: UUID, group_id: UUID) -> HoldingRollbackPreview:
    from app.services.holdings_write import format_holdings_version

    version = session.scalars(select(User.holdings_version).where(User.id == user_id)).one()
    current = snapshot(session.scalars(select(Holding).where(Holding.user_id == user_id)).all())
    group, target, count = rollback_target(current, retained_groups(session, user_id), group_id)
    return HoldingRollbackPreview(
        target_changed_at=group.created_at.astimezone(ET),
        groups=count,
        restored=len(target.keys() - current.keys()),
        removed=len(current.keys() - target.keys()),
        modified=sum(target[hid] != current[hid] for hid in target.keys() & current.keys()),
        version=format_holdings_version(version),
    )
