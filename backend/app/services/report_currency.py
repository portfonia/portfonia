"""Apply a report/base currency preference change and append the audit row.

Issue #372 slice A: write-on-change only. Never rewrites
`portfolio_value_snapshots`.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.report_currency_change import (
    VALID_REPORT_CURRENCY_CHANGE_SOURCES,
    ReportCurrencyChange,
)
from app.models.user import User


def apply_report_currency_change(
    session: Session,
    user: User,
    new_currency: str,
    *,
    source: str,
    actor_user_id: uuid.UUID | None,
) -> bool:
    """Set `users.base_currency` and insert an audit row when it changes.

    Returns True if an audit row was appended. Does not commit. A no-op
    (old == new) leaves the session untouched and returns False.
    """
    if source not in VALID_REPORT_CURRENCY_CHANGE_SOURCES:
        raise ValueError(f"unrecognized report-currency change source {source!r}")
    old_currency = user.base_currency
    if old_currency == new_currency:
        return False
    session.add(
        ReportCurrencyChange(
            user_id=user.id,
            old_currency=old_currency,
            new_currency=new_currency,
            source=source,
            actor_user_id=actor_user_id,
        )
    )
    user.base_currency = new_currency
    return True
