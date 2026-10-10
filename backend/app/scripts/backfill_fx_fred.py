"""One-time FRED H.10 seed for the dot-com and 2008 stress windows.

Dry run: python -m app.scripts.backfill_fx_fred
Apply:   python -m app.scripts.backfill_fx_fred --apply
Existing FX rows are never overwritten.
"""

import argparse
import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.fx_rate import FxRate
from app.services.jade_replay_config import CARRY_DAYS, SCENARIOS

FRED_SERIES: dict[str, tuple[str, bool]] = {
    "USDEUR": ("DEXUSEU", True),
    "USDGBP": ("DEXUSUK", True),
    "USDAUD": ("DEXUSAL", True),
    "USDNZD": ("DEXUSNZ", True),
    "USDJPY": ("DEXJPUS", False),
    "USDCAD": ("DEXCAUS", False),
    "USDCHF": ("DEXSZUS", False),
    "USDHKD": ("DEXHKUS", False),
    "USDSGD": ("DEXSIUS", False),
    "USDKRW": ("DEXKOUS", False),
    "USDTWD": ("DEXTAUS", False),
    "USDCNY": ("DEXCHUS", False),
}
DERIVED: dict[str, tuple[str, Decimal, str]] = {
    "USDCNH": ("USDCNY", Decimal("1"), "fred_cny"),
    "USDMOP": ("USDHKD", Decimal("1.03"), "fred_hkd_peg"),
}
_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_INSERT_BATCH_SIZE = 5000


def _fetch(series: str, invert: bool, start: date, end: date) -> list[tuple[date, Decimal]]:
    response = httpx.get(
        _URL, params={"id": series, "cosd": start.isoformat(), "coed": end.isoformat()}, timeout=30
    )
    response.raise_for_status()
    reader = csv.reader(io.StringIO(response.text))
    next(reader)
    points = []
    for day_text, text in reader:
        if text in (".", ""):
            continue
        day = date.fromisoformat(day_text)
        if start <= day <= end:
            rate = Decimal(1) / Decimal(text) if invert else Decimal(text)
            points.append((day, rate))
    return points


def run(session: Session, *, apply: bool = False) -> int:
    windows = [
        (s.start - timedelta(days=CARRY_DAYS), s.end) for s in SCENARIOS if s.fx_source == "fred"
    ]
    start, end = min(a for a, _ in windows), max(b for _, b in windows)
    inside = or_(*(FxRate.rate_date.between(a, b) for a, b in windows))
    fetched_at = datetime.now(tz=UTC)
    rows: list[dict[str, object]] = []
    failed = False
    for pair, (series, invert) in FRED_SERIES.items():
        try:
            points = [
                (day, rate)
                for day, rate in _fetch(series, invert, start, end)
                if any(a <= day <= b for a, b in windows)
            ]
            earliest = session.scalar(
                select(FxRate).where(FxRate.pair == pair).order_by(FxRate.rate_date).limit(1)
            )
            if earliest is not None:
                check = _fetch(
                    series,
                    invert,
                    earliest.rate_date - timedelta(days=CARRY_DAYS),
                    earliest.rate_date,
                )
                latest = max(check, key=lambda p: p[0])[1] if check else None
                diff = (latest / earliest.rate - 1) * 100 if latest is not None else None
                print(
                    f"check {pair} {earliest.rate_date}: fred={latest} stored={earliest.rate} diff={diff}%"
                )
        except httpx.HTTPError as exc:
            print(f"[ERR] {series}: {exc}")
            failed = True
            continue
        pair_points = [
            (pair, Decimal(1), "fred"),
            *(
                (derived, factor, source)
                for derived, (base, factor, source) in DERIVED.items()
                if base == pair
            ),
        ]
        for target, factor, source in pair_points:
            existing = session.scalar(
                select(func.count()).select_from(FxRate).where(FxRate.pair == target, inside)
            )
            first = min((d for d, _ in points), default=None)
            last = max((d for d, _ in points), default=None)
            print(
                f"[i] {target} {source} rows={len(points)} first={first} last={last} existing_in_windows={existing}"
            )
            rows.extend(
                {
                    "pair": target,
                    "rate": rate * factor,
                    "rate_date": day,
                    "source": source,
                    "fetched_at": fetched_at,
                }
                for day, rate in points
            )
    if apply:
        for offset in range(0, len(rows), _INSERT_BATCH_SIZE):
            chunk = rows[offset : offset + _INSERT_BATCH_SIZE]
            session.execute(
                insert(FxRate)
                .values(chunk)
                .on_conflict_do_nothing(constraint="uq_fx_rates_pair_rate_date")
            )
        session.commit()
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Insert rows inside the two windows; preserve every existing row",
    )
    args = parser.parse_args()
    with SessionLocal() as session:
        return run(session, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
