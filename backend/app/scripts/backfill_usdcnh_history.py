"""One-off historical gap-fill for USDCNH only (issue #406).

`backfill_fx_rates.py` (issue #398) seeded ~5 years of history for every
`_PAIRS` entry from yfinance, but yfinance classifies `USDCNH=X` as a
limited-history quote type -- that run only produced 25 real days
(2026-08-05 onward). Twelve Data's free tier was confirmed live to carry
full multi-year USD/CNH daily history (issue #406's Exploration). This
script fills ONLY the gap strictly before the earliest existing `USDCNH`
row already in `fx_rates` -- it never touches or overlaps the existing
yfinance-sourced rows, so today's daily `capture_fx_task`/`update_fx_rates`
freshness path (still yfinance-only, unchanged) keeps owning the recent
end of the series untouched by this script.

Idempotent: re-running upserts on `(pair, rate_date)`, same convention as
`backfill_fx_rates.py`. Rows are tagged `source="twelvedata"` so this
provenance is visible in the table itself, distinct from the `"yfinance"`
rows on either side of the boundary.

    python -m app.scripts.backfill_usdcnh_history            # dry run
    python -m app.scripts.backfill_usdcnh_history --apply    # commit
    python -m app.scripts.backfill_usdcnh_history --years 3  # shorter window
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.fx_rate import FxRate
from app.services._twelvedata import fetch_daily_history

_PAIR = "USDCNH"
_SYMBOL = "USD/CNH"


def _existing_earliest_date(session: Session) -> date | None:
    """Earliest `USDCNH` row NOT written by this script.

    Excludes `source == "twelvedata"` deliberately (PR #411 review): without
    this exclusion, a rerun sees this script's own first-run output as the
    new "earliest existing row" and shifts the entire target window another
    `years` further into the past every time it runs, expanding forever
    instead of idempotently re-filling the same fixed gap. Only the
    pre-existing (yfinance-sourced) boundary may ever determine the window.
    """
    return session.execute(
        select(func.min(FxRate.rate_date)).where(
            FxRate.pair == _PAIR, FxRate.source != "twelvedata"
        )
    ).scalar_one_or_none()


def backfill_usdcnh_history(session: Session, *, years: int, apply_changes: bool) -> int:
    earliest = _existing_earliest_date(session)
    end_date = (earliest - timedelta(days=1)) if earliest is not None else date.today()
    start_date = end_date - timedelta(days=365 * years)

    if end_date < start_date:
        print(f"[OK] nothing to backfill: end_date {end_date} precedes start_date {start_date}")
        return 0

    api_key = get_settings().TWELVEDATA_API_KEY
    if api_key is None:
        raise SystemExit("TWELVEDATA_API_KEY is not set -- cannot fetch from Twelve Data")

    points = fetch_daily_history(_SYMBOL, start_date, end_date, api_key.get_secret_value())
    # Defensive: never write a row on or after `end_date + 1`, regardless of
    # what the API returns -- the existing yfinance-sourced rows starting at
    # `earliest` are strictly out of scope for this script.
    points = [(d, rate) for d, rate in points if d <= end_date]

    if not points:
        print(f"[OK] twelvedata returned no rows in [{start_date}, {end_date}]")
        return 0

    fetched_at = datetime.now(tz=UTC)
    rows: list[dict[str, object]] = [
        {
            "pair": _PAIR,
            "rate": rate,
            "rate_date": rate_date,
            "source": "twelvedata",
            "fetched_at": fetched_at,
        }
        for rate_date, rate in points
    ]

    tag = "APPLY" if apply_changes else "DRY-RUN"
    print(
        f"[{tag}] {len(rows)} row(s) for {_PAIR} covering "
        f"{points[0][0]} to {points[-1][0]} (existing data starts {earliest})"
    )

    if not apply_changes:
        return len(rows)

    base = insert(FxRate).values(rows)
    stmt = base.on_conflict_do_update(
        constraint="uq_fx_rates_pair_rate_date",
        set_={"rate": base.excluded.rate, "fetched_at": base.excluded.fetched_at},
    ).returning(FxRate.id)
    written = len(session.execute(stmt).fetchall())
    print(f"[OK] wrote {written} row(s) for {_PAIR}")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as session:
        written = backfill_usdcnh_history(session, years=args.years, apply_changes=args.apply)
        if args.apply and written:
            session.commit()


if __name__ == "__main__":
    main()
