"""Grant credits to existing users, without writing during the default dry run.

Run after deploying the schema and write path:

    python -m app.scripts.backfill_signup_grants
    python -m app.scripts.backfill_signup_grants --apply

Production execution requires separate owner authorization.
"""

from __future__ import annotations

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.credit_ledger import CreditLedgerEntry
from app.models.user import User
from app.services.credit_ledger import grant_signup_credits, signup_grant_key


def backfill_signup_grants(session: Session, *, apply_changes: bool) -> int:
    users = session.execute(select(User).order_by(User.created_at)).scalars().all()
    count = 0
    for user in users:
        if apply_changes:
            entry = grant_signup_credits(
                session, user, note="backfill: registered before credit ledger"
            )
            session.commit()
            granted = entry is not None
            state = "granted" if granted else "skipped"
        else:
            key = signup_grant_key(user.email)
            exists = session.execute(
                select(CreditLedgerEntry.id).where(CreditLedgerEntry.idempotency_key == key)
            ).first()
            granted = exists is None
            state = "would-grant" if granted else "already-granted"
        count += int(granted)
        print(f"{user.id}  {user.email}  {state}")
    print(f"total users={len(users)} {'granted' if apply_changes else 'would-grant'}={count}")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit each user's grant")
    args = parser.parse_args()
    with SessionLocal() as session:
        backfill_signup_grants(session, apply_changes=args.apply)


if __name__ == "__main__":
    main()
