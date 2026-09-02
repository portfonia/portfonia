"""Backfill ~1 year of daily OHLCV closes into `price_snapshots` (#4).

The capture layer only started accruing closes from its first run, so the longer
technical-position windows (50/200-day average, 52-week range) have no history
until enough sessions pass. This one-shot backfill seeds a year of closes so those
metrics populate immediately; it also lengthens the series the anomaly baseline can
draw on.

Idempotent: reuses `capture_prices(..., session_node="close")`, whose upsert is keyed
on (ticker, market, session_node, trade_date), so re-running overwrites rather than
duplicates. Run once after seeding holdings:

    python -m app.scripts.backfill_ohlcv
"""

from __future__ import annotations

from app.core.database import SessionLocal
from app.services.markets import CAPTURE_MARKET_ORDER
from app.services.price_capture import capture_prices

# One trading year is ~252 sessions; 420 calendar days comfortably covers it.
_LOOKBACK_DAYS = 420
_MARKETS = CAPTURE_MARKET_ORDER


def main() -> None:
    with SessionLocal() as session:
        total = 0
        for market in _MARKETS:
            written = capture_prices(session, market, "close", lookback_days=_LOOKBACK_DAYS)
            print(f"[OK] {market}: {written} close bars upserted")
            total += written
    print(f"[OK] backfill complete: {total} bars across {len(_MARKETS)} markets")


if __name__ == "__main__":
    main()
