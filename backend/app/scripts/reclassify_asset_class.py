"""One-off: re-derive `asset_class` for stored etf/fund holdings (issue #788).

Rows saved before their identifier was added to `config/ticker_asset_class.yml`
keep the `EQUITY_BROAD` catch-all. After the mapping is deployed, this applies
the current classification to them. No `holdings_version` bump, no change
history, no alerts, no snapshot changes.

    python -m app.scripts.reclassify_asset_class          # dry run
    python -m app.scripts.reclassify_asset_class --apply  # commit changes
"""

from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.holding import Holding
from app.services.holding_parser import _classify_asset_class
from app.services.ticker_leverage import load_leverage_entries


def reclassify_asset_class(session: Session, *, apply_changes: bool) -> int:
    classes = {
        t: e.asset_class
        for t, e in load_leverage_entries(session).items()
        if e.asset_class is not None
    }
    rows = session.query(Holding).filter(Holding.asset_type.in_(("etf", "fund"))).all()
    changed = 0
    for h in rows:
        new = _classify_asset_class(
            {"ticker": h.ticker, "fund_code": h.fund_code, "asset_type": h.asset_type}, classes
        )
        if new == h.asset_class:
            continue
        changed += 1
        print(f"holding {h.id} {h.ticker or h.fund_code}: {h.asset_class} -> {new}")
        if apply_changes:
            h.asset_class = new
    print(f"{changed} row(s) {'updated' if apply_changes else 'would change'}")
    return changed


def main() -> None:
    apply_changes = "--apply" in sys.argv
    with SessionLocal() as session:
        reclassify_asset_class(session, apply_changes=apply_changes)
        if apply_changes:
            session.commit()
        else:
            session.rollback()
            print("dry run, no changes written")


if __name__ == "__main__":
    main()
