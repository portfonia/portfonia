"""One-off ~5-year history seed for `fx_rates` (issue #398).

Mirrors `backfill_benchmark_prices.py`: observed market data, not portfolio
composition-replay, no `is_backfilled`. Covers every pair in
`app.services.fx_fetcher._PAIRS`. Idempotent upsert on `(pair, rate_date)`.
Uses yfinance period `f"{years}y"` (never a huge `Nd`). Daily
`capture_fx_task` / `update_fx_rates` remains the ongoing freshness path.

    python -m app.scripts.backfill_fx_rates            # 5 years (default)
    python -m app.scripts.backfill_fx_rates --years 3
"""

from __future__ import annotations

import argparse

from app.core.database import SessionLocal
from app.services.fx_fetcher import backfill_fx_rates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=5)
    args = parser.parse_args()

    with SessionLocal() as session:
        backfill_fx_rates(session, years=args.years)
        session.commit()


if __name__ == "__main__":
    main()
