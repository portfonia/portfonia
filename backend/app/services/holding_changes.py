"""Record encrypted field diffs in the holdings write transaction."""

import json
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET, today_et
from app.models.api_token import ApiToken
from app.models.holding import Holding
from app.models.holding_change_group import HoldingChangeGroup
from app.schemas.agent_holdings import fixed
from app.schemas.holding_changes import ChangedRow, ChangeToken, HoldingChangeOut, HoldingChangesOut

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
    source: Literal["web", "file", "agent"],
    token_id: UUID | None = None,
) -> None:
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
        return
    token = session.get(ApiToken, token_id) if source == "agent" and token_id is not None else None
    session.add(
        HoldingChangeGroup(
            user_id=user_id,
            source=source,
            token_name=token.name if token else None,
            token_prefix=token.token_prefix if token else None,
            changes=json.dumps(entries),
        )
    )


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
                source=cast(Literal["web", "file", "agent"], row.source),
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
