"""Resolve/create `accounts` rows for a user's holdings (issue #129 B7).

Both `POST /holdings/confirm` and `app/scripts/seed.py` fully replace a
user's holdings on every write (delete then reinsert) — this is the only
holdings-write path that exists until stage C's inline entry form. Without
this module, B7's migration backfill goes stale the moment anyone
re-uploads: every newly-inserted holding gets `account_id=None`, and the
migration's `accounts` rows become unreferenced ghosts (review, PR #247).

Dedup key matches the migration's backfill exactly: decrypted
`(broker, account, portfolio)` plaintext tuple, not the ciphertext columns
— Fernet's random IV means two encryptions of identical plaintext never
match, so this must compare decrypted values fetched into Python, same as
every other cross-row comparison on these encrypted columns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account


def resolve_accounts_for_holdings(
    session: Session,
    user_id: UUID,
    rows: list[tuple[str | None, str | None, str | None]],
) -> list[UUID | None]:
    """For each `(broker, account, portfolio)` plaintext tuple in `rows`
    (same order as the caller's holdings), return the matching
    `accounts.id` — reusing an existing non-archived account for this user
    when the tuple matches, creating one otherwise. `broker=None` in ->
    `None` out (no institution to normalize a broker-less holding against,
    matching the migration backfill's rule; `accounts.broker` is NOT NULL).

    Also archives (never deletes — this is the user's own account history,
    and `accounts.archived_at` exists for exactly this) any of this user's
    non-archived accounts that end up referenced by none of `rows` — the
    caller is always replacing the user's full holdings set, so an account
    with no match in the new set genuinely has nothing pointing at it.
    """
    existing = list(
        session.execute(
            select(Account).where(Account.user_id == user_id, Account.archived_at.is_(None))
        ).scalars()
    )
    by_key: dict[tuple[str, str | None, str | None], Account] = {
        (a.broker, a.account, a.portfolio): a for a in existing
    }
    referenced: set[UUID] = set()
    result: list[UUID | None] = []
    for broker, account, portfolio in rows:
        if broker is None:
            result.append(None)
            continue
        key = (broker, account, portfolio)
        row = by_key.get(key)
        if row is None:
            row = Account(user_id=user_id, broker=broker, account=account, portfolio=portfolio)
            session.add(row)
            session.flush()
            by_key[key] = row
        result.append(row.id)
        referenced.add(row.id)

    now = datetime.now(tz=UTC)
    for acc in existing:
        if acc.id not in referenced:
            acc.archived_at = now
    return result
