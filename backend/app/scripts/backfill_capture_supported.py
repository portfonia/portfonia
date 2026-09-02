"""One-off backfill: resolve `capture_supported` for pre-existing
`market="Other"` holdings (issue #313 item 4).

Migration `c7d8e9f0a1b2` (issue #311) added `capture_supported` defaulting
True for every existing row, including ones already stored as
`market="Other"` — the migration runs raw SQL against Fernet-encrypted
ciphertext and cannot decrypt `ticker` to re-resolve it. This script runs
the same resolution `confirm_holdings` already applies to every fresh
upload/re-confirm (`resolve_holding_market`), scoped to existing
`market="Other"` rows only: a ticker that now resolves into one of the 7
capture buckets (e.g. a UK/Europe/Japan/Korea holding uploaded before #311
shipped those nodes) gets reclassified with `capture_supported=True`; a
ticker that still doesn't resolve gets `capture_supported=False` so section
1 renders "[market not supported]" instead of sitting in "[price
unavailable]" Other limbo indefinitely. A row whose ticker already resolved
correctly is left untouched.

Deliberately NOT a migration — the issue is explicit that this must not be
a silent rewrite bundled into a schema deploy — and NOT a new `/admin/*`
bulk endpoint: a re-upload/re-confirm already reaches the same resolution
for any individual user, and a one-off ORM-level backfill script for a
pre-existing-row gap has an established project precedent (see the
email-verification backfill note in `docs/mechanisms/email-verification.md`,
issue #260). Idempotent: re-running is a no-op for every row already
correctly resolved. Every change is printed; nothing here is silent.

Run once, after deploying #313:

    python -m app.scripts.backfill_capture_supported          # dry run
    python -m app.scripts.backfill_capture_supported --apply  # commit changes
"""

from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.holding import Holding
from app.services.markets import resolve_holding_market


def backfill_capture_supported(session: Session, *, apply_changes: bool) -> int:
    """Re-resolve every `market="Other"` holding; return the count changed.

    Mutates ORM rows in place when `apply_changes` (caller commits) — a dry
    run only prints what would change, matching `apply_changes=False`'s
    contract of leaving `session` untouched.
    """
    rows = session.query(Holding).filter(Holding.market == "Other").all()
    changed = 0
    for h in rows:
        resolved_market, capture_ok = resolve_holding_market(
            ticker=h.ticker,
            declared_market=h.market,
            fund_code=h.fund_code,
            asset_type=h.asset_type,
            pricing_mode=h.pricing_mode,
        )
        if resolved_market == h.market and capture_ok == h.capture_supported:
            continue
        changed += 1
        tag = "APPLY" if apply_changes else "DRY-RUN"
        print(
            f"[{tag}] holding {h.id} (user {h.user_id}): "
            f"market {h.market!r} -> {resolved_market!r}, "
            f"capture_supported {h.capture_supported} -> {capture_ok}"
        )
        if apply_changes:
            h.market = resolved_market
            h.capture_supported = capture_ok
    verb = "updated" if apply_changes else "would change"
    print(f"[OK] {changed} row(s) {verb} out of {len(rows)} market='Other' holding(s) scanned")
    return changed


def main() -> None:
    apply_changes = "--apply" in sys.argv
    with SessionLocal() as session:
        changed = backfill_capture_supported(session, apply_changes=apply_changes)
        if apply_changes and changed:
            session.commit()


if __name__ == "__main__":
    main()
