"""One-off ~5-year history seed for the three Portfolio Performance
benchmark indexes (issue #360 Phase 1). Unlike
`backfill_portfolio_value_history.py`, this is a normal historical time
series fetch — no approximation, no per-user scope, safe to re-run
(idempotent upsert on `(index_code, price_date)`).

    python -m app.scripts.backfill_benchmark_prices            # 5 years (default)
    python -m app.scripts.backfill_benchmark_prices --years 3
"""

from __future__ import annotations

import argparse

from app.core.database import SessionLocal
from app.services.benchmark_prices import backfill_benchmark_prices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=5)
    args = parser.parse_args()

    with SessionLocal() as session:
        backfill_benchmark_prices(session, years=args.years)
        session.commit()


if __name__ == "__main__":
    main()
