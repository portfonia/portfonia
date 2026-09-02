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


def _normalize(value: str | None) -> str | None:
    """Blank/whitespace-only collapses to None (review, PR #247 round 2):
    `report_sections.py` (`h.get("broker") or "Other"`) and
    `holding_parser._summarize` (`(row.broker or "").strip() or "Other"`)
    both already treat an empty/whitespace broker as equivalent to no
    broker. Without this, `broker=""` or `broker="  "` slipped past the
    `is None` check, creating a real `accounts` row (`broker` is NOT NULL,
    not "non-blank") while the holding still rendered under "Other" —
    and `" IBKR "` vs `"IBKR"` would fan the same custodian out into two
    accounts, the opposite of the migration's grouping intent.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def resolve_accounts_for_holdings(
    session: Session,
    user_id: UUID,
    rows: list[tuple[str | None, str | None, str | None]],
    *,
    archive_unreferenced: bool = True,
) -> list[UUID | None]:
    """For each `(broker, account, portfolio)` plaintext tuple in `rows`
    (same order as the caller's holdings), return the matching
    `accounts.id` — reusing an existing non-archived account for this user
    when the tuple matches, creating one otherwise. Each value is first
    normalized via `_normalize` (blank/whitespace-only -> None, surrounding
    whitespace stripped). A normalized `broker=None` -> `None` out (no
    institution to normalize a broker-less holding against, matching the
    migration backfill's rule; `accounts.broker` is NOT NULL).

    When `archive_unreferenced` is True (the default, for a full-replace
    confirm), also archives (never deletes — this is the user's own
    account history, and `accounts.archived_at` exists for exactly this)
    any of this user's non-archived accounts that end up referenced by
    none of `rows`. Single-row POST/PATCH and confirm-append pass
    False so other lots' accounts are not archived.
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
    for raw_broker, raw_account, raw_portfolio in rows:
        broker = _normalize(raw_broker)
        account = _normalize(raw_account)
        portfolio = _normalize(raw_portfolio)
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

    if archive_unreferenced:
        now = datetime.now(tz=UTC)
        for acc in existing:
            if acc.id not in referenced:
                acc.archived_at = now
    return result
