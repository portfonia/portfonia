"""Activate existing subscriptions at launch, before the next scheduled batch.

Dry run by default; --apply commits one user at a time. Production execution
(including dry run) requires separate, current owner authorization.
"""

import argparse
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.timezones import today_et
from app.models.user import User
from app.services.subscription import PLAN_FEES, activate_existing, has_verified_email


def activate_existing_subscriptions(session: Session, *, apply_changes: bool, today: date) -> None:
    ids = sorted(session.scalars(select(User.id)).all())
    for user_id in ids:
        user = session.get(User, user_id)
        assert user is not None
        email, plan = user.email, user.report_cadence
        eligible = user.status == "active" and has_verified_email(user) and plan in PLAN_FEES
        if apply_changes:
            outcome = activate_existing(session, user, today)
            session.commit()
        elif user.subscription_status != "inactive":
            outcome = "skipped"
        elif eligible:
            outcome = (
                "would-activate"
                if user.credit_cash_balance + user.credit_gift_balance >= PLAN_FEES[plan]
                else "insufficient"
            )
        else:
            outcome = "inactive"
        amount = PLAN_FEES[plan] if outcome in ("activated", "would-activate") else 0
        print(f"{email}  {outcome}  {plan}  {amount:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit each user's activation")
    args = parser.parse_args()
    with SessionLocal() as session:
        activate_existing_subscriptions(session, apply_changes=args.apply, today=today_et())


if __name__ == "__main__":
    main()
