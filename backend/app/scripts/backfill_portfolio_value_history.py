"""One-off backfill: approximate portfolio value history for one user, on
FIRST ENABLE of the Portfolio Performance feature (issue #360 Phase 1, D2
amendment).

Freezes the user's CURRENT holdings composition and replays it backward
against historical prices/FX, day by day (business days only), from a
start date to yesterday (today is left to the daily task). Every written
row is flagged `is_backfilled=True`; a day whose FX rate can't be resolved
even with the 10-day historical lookback falls back to THIS SCRIPT'S OWN
run-time (today's) FX rate and is additionally flagged `is_fx_fallback=True`
(D6) — never done by the live daily task.

Refuses to run a second time for a user who already has real (non-backfill)
snapshot history, unless `--force` is passed (with an explicit warning) —
this backfill is a one-shot approximation for a cold start, not a repeatable
resync. Never uses `holding.created_at` as the start date (D2: a
replace-import deletes and recreates rows, so `created_at` does not mean
"date first owned") — the start date is instead the earliest date any
currently-held auto-priced ticker has usable captured-price history, capped
at `--years` (default 5).

Mirrors `backfill_capture_supported.py`'s dry-run-first pattern:

    python -m app.scripts.backfill_portfolio_value_history --user-id <uuid>          # dry run
    python -m app.scripts.backfill_portfolio_value_history --user-id <uuid> --apply  # commit
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.holding import Holding
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.price_snapshot import PriceSnapshot
from app.services._yfinance import fetch_last_close
from app.services.instrument_symbols import normalize_legacy_ticker
from app.services.markets import is_capture_supported
from app.services.portfolio_history import required_fx_pairs, write_user_snapshot
from app.services.user_scope import report_currency_for

_DEFAULT_CAP_YEARS = 5


def _existing_real_history(session: Session, user_id: UUID) -> int:
    """Count of REAL (non-backfill, i.e. daily-task-produced) rows only.

    Deliberately not "any snapshot row at all" (review 5124107298 finding
    4 raised this as a possible gap): the D2 amendment's refuse condition
    is explicitly scoped to "real (non-backfill) history already exists" —
    a user with ONLY prior backfill rows and no real daily-task history yet
    is exactly the "still first enable" state this script must remain
    re-runnable for. `_insert_rows_skip_existing`'s `ON CONFLICT DO
    NOTHING` already makes a second run over that state a safe no-op
    (idempotent skip, matching the amendment's rule that a script rerun
    must never overwrite history already written to the database), so
    widening this count to include prior backfill rows would make the
    script LESS resumable than the spec asks for, not more correct.
    """
    return int(
        session.execute(
            select(func.count())
            .select_from(PortfolioValueSnapshot)
            .where(
                PortfolioValueSnapshot.user_id == user_id,
                ~PortfolioValueSnapshot.is_backfilled,
            )
        ).scalar_one()
    )


def _earliest_usable_start(
    session: Session, holdings: list[Holding], cap_years: int, today: date
) -> date:
    """Earliest date with usable captured-price history among currently
    held auto-priced tickers, capped at `cap_years` back from `today`.
    Never `holding.created_at` (D2) — that column does not survive a
    replace-import. A holding-free or all-manual book (no auto ticker to
    anchor on) falls back straight to the cap."""
    cap_date = today - timedelta(days=365 * cap_years)
    keys = {
        normalize_legacy_ticker(h.ticker or h.fund_code or "")
        for h in holdings
        if h.pricing_mode == "auto" and is_capture_supported(h) and (h.ticker or h.fund_code)
    }
    if not keys:
        return cap_date
    earliest = session.execute(
        select(func.min(PriceSnapshot.trade_date)).where(
            PriceSnapshot.ticker.in_(keys), PriceSnapshot.session_node == "close"
        )
    ).scalar_one_or_none()
    if earliest is None or earliest < cap_date:
        return cap_date
    return earliest


def _business_days(start: date, end: date) -> list[date]:
    days = []
    cursor = start
    while cursor < end:  # end (today) is left to the live daily task
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _run_time_fx_rates(holdings: list[Holding], base_currency: str) -> dict[str, Decimal]:
    """Today's actual FX rates, fetched once, for D6's run-time fallback."""
    pairs = required_fx_pairs(holdings, base_currency)
    if not pairs:
        return {}
    yf_symbols = {pair: f"{pair}=X" for pair in pairs}
    points = fetch_last_close(list(yf_symbols.values()))
    rates: dict[str, Decimal] = {}
    inverse = {v: k for k, v in yf_symbols.items()}
    for yf_symbol, (rate, _as_of) in points.items():
        pair = inverse.get(yf_symbol)
        if pair:
            rates[pair] = Decimal(str(rate))
    return rates


def backfill_portfolio_value_history(
    session: Session,
    user_id: UUID,
    *,
    apply_changes: bool,
    force: bool = False,
    cap_years: int = _DEFAULT_CAP_YEARS,
    today: date | None = None,
) -> dict[str, int]:
    today = today or date.today()
    existing = _existing_real_history(session, user_id)
    if existing and not force:
        print(
            f"[REFUSED] user {user_id} already has {existing} real (non-backfill) "
            "snapshot row(s) — this backfill only runs on first enable. Pass "
            "--force to override (re-approximates history; does not touch "
            "already-written rows either way)."
        )
        return {"days_written": 0, "rows_written": 0, "refused": 1}

    holdings = list(session.execute(select(Holding).where(Holding.user_id == user_id)).scalars())
    if not holdings:
        print(f"[OK] user {user_id} has no holdings — nothing to backfill")
        return {"days_written": 0, "rows_written": 0, "refused": 0}

    base_currency = report_currency_for(session, user_id, "USD")
    start_date = _earliest_usable_start(session, holdings, cap_years, today)
    days = _business_days(start_date, today)
    # Only fetched when actually writing — a dry run must have zero
    # side effects, including no live network call to price providers.
    run_time_rates = _run_time_fx_rates(holdings, base_currency) if apply_changes else {}

    tag = "APPLY" if apply_changes else "DRY-RUN"
    total_rows = 0
    days_written = 0
    for day in days:
        if apply_changes:
            written, status = write_user_snapshot(
                session,
                user_id,
                day,
                is_backfilled=True,
                upsert=False,
                run_time_fx_rates=run_time_rates,
            )
            total_rows += written
            if status == "complete":
                days_written += 1
    print(
        f"[{tag}] user {user_id}: backfill range {start_date.isoformat()} .. "
        f"{(today - timedelta(days=1)).isoformat()} ({len(days)} business day(s)), "
        f"base_currency={base_currency}"
    )
    if not apply_changes:
        print(
            f"[DRY-RUN] would write approximate snapshots for {len(holdings)} "
            f"holding(s) across {len(days)} business day(s); re-run with --apply to commit"
        )
    else:
        print(f"[APPLY] wrote {total_rows} row(s) across {days_written} complete day(s)")
    return {"days_written": days_written, "rows_written": total_rows, "refused": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=str)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--years", type=int, default=_DEFAULT_CAP_YEARS)
    args = parser.parse_args()

    user_id = UUID(args.user_id)
    with SessionLocal() as session:
        result = backfill_portfolio_value_history(
            session,
            user_id,
            apply_changes=args.apply,
            force=args.force,
            cap_years=args.years,
        )
        if args.apply and not result["refused"]:
            session.commit()


if __name__ == "__main__":
    main()
