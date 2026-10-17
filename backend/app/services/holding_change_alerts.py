"""Evaluate committed Agent/file change groups without delaying holdings writes."""

import logging
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.core.rate_limit import get_backend
from app.core.timezones import ET, today_et
from app.models.holding import Holding
from app.models.holding_change_group import HoldingChangeGroup
from app.models.user import User
from app.services import email_sender, holding_changes
from app.services.api_token_revoke_link import create_link
from app.services.instrument_symbols import normalize_legacy_ticker
from app.services.markets import resolve_holding_market
from app.services.portfolio_calculator import (
    PortfolioSnapshot,
    _holding_value,
    _latest_captured_closes,
    _load_fx_rates,
)
from app.services.user_directory import recipient_email_with_purpose

logger = logging.getLogger(__name__)


def enqueue_change_alert(user_id: UUID, group_id: UUID) -> None:
    from app.tasks.notification_tasks import send_holdings_change_alert_task

    try:
        send_holdings_change_alert_task.delay(str(user_id), str(group_id))
    except Exception:
        logger.error("Holdings change alert enqueue failed user_id=%s", user_id)


def _rows(state: holding_changes.Snapshot) -> list[Holding]:
    result = []
    for hid, values in state.items():
        data = {
            key: Decimal(str(value))
            if key in ("shares", "avg_cost", "current_value") and value is not None
            else value
            for key, value in values.items()
        }
        row = Holding(id=hid, **data)
        row.capture_supported = resolve_holding_market(
            ticker=row.ticker,
            declared_market=row.market,
            fund_code=row.fund_code,
            asset_type=row.asset_type,
            pricing_mode=row.pricing_mode,
        )[1]
        result.append(row)
    return result


def evaluate_change_alert(
    session: Session, uid: UUID, group_id: UUID, *, claimed: bool = False
) -> bool:
    """Return False only for a failed delivery eligible for the task's retry."""
    group = session.get(HoldingChangeGroup, group_id)
    if group is None or group.user_id != uid or group.source not in ("agent", "file"):
        return True
    current = holding_changes.snapshot(
        session.scalars(select(Holding).where(Holding.user_id == uid)).all()
    )
    groups = list(
        session.scalars(
            select(HoldingChangeGroup)
            .where(
                HoldingChangeGroup.user_id == uid,
                tuple_(HoldingChangeGroup.created_at, HoldingChangeGroup.id)
                >= (group.created_at, group.id),
            )
            .order_by(HoldingChangeGroup.created_at.desc(), HoldingChangeGroup.id.desc())
        ).all()
    )
    states = holding_changes.replay(current, groups)
    before = states[-1]
    after = current if len(states) == 1 else states[-2]
    if before is None or after is None:
        logger.info("Holdings change alert skipped: replay blocked user_id=%s", uid)
        return True
    rows_before, rows_after = len(before), len(after)
    signals = []
    if rows_before > 0 and (rows_before - rows_after) * 100 >= 30 * rows_before:
        signals.append("deletion")
    user = session.get(User, uid)
    assert user is not None
    before_rows, after_rows = _rows(before), _rows(after)
    keys = {
        normalize_legacy_ticker(row.ticker or row.fund_code or "")
        for row in [*before_rows, *after_rows]
        if row.ticker or row.fund_code
    }
    closes = _latest_captured_closes(session, only=keys)
    fx, _, _ = _load_fx_rates(session)
    snapshot = PortfolioSnapshot(base_currency=user.base_currency)
    totals = [
        sum(
            (
                _holding_value(row, user.base_currency, fx, closes, today_et(), snapshot)[1]
                or Decimal(0)
                for row in rows
            ),
            Decimal(0),
        )
        for rows in (before_rows, after_rows)
    ]
    v_before, v_after = totals
    value_change_pct = None
    if v_before > 0:
        if v_after * 100 <= 70 * v_before:
            signals.append("drop")
        if v_after * 100 >= 200 * v_before:
            signals.append("rise")
        value_change_pct = round((v_after - v_before) * 100 / v_before)
    if not signals:
        return True
    changed_at = group.created_at.astimezone(ET)
    if not claimed:
        try:
            if not get_backend().set_nx(f"holdings:alert:{uid}:{changed_at.date()}", 129600):
                return True
        except Exception:
            logger.error("Holdings change alert marker failed user_id=%s", uid)
            return True
    recipient = recipient_email_with_purpose(session, uid)
    if recipient is None:
        logger.info("Holdings change alert skipped: no verified recipient user_id=%s", uid)
        return True
    revoke_url = (
        f"https://portfonia.com/agent/revoke?t={create_link(uid)}"
        if group.source == "agent"
        else None
    )
    return email_sender.send_holdings_change_alert(
        recipient[0],
        locale=user.locale,
        signals=signals,
        changed_at_et=changed_at.strftime("%Y-%m-%d %H:%M ET"),
        source=group.source,
        token=(cast(str, group.token_name), cast(str, group.token_prefix))
        if group.source == "agent"
        else None,
        rows_before=rows_before,
        rows_after=rows_after,
        value_change_pct=value_change_pct,
        revoke_url=revoke_url,
    )
