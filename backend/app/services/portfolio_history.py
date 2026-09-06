"""Daily portfolio value snapshot writer + one-off backfill core (issue #360
Phase 1). Shared by `app/tasks/capture_tasks.py` (daily beat task),
`app/scripts/backfill_portfolio_value_history.py` (one-off first-enable
backfill), and read by `app/services/portfolio_performance.py`.

Valuation here deliberately does NOT call `compute_portfolio`/
`portfolio_calculator` (CLAUDE.md: do not change summary's
`capture_supported=False` exclusion behavior, and this module's D5 rules
differ from summary's on purpose — see `_local_value_for_holding`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import partial

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.models.portfolio_snapshot_batch import PortfolioSnapshotBatch
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.price_snapshot import PriceSnapshot
from app.models.user import User
from app.services.fx_conversion import CURRENCY_TO_FX_PAIR, fx_multiplier, to_base
from app.services.instrument_symbols import normalize_legacy_ticker
from app.services.markets import is_capture_supported
from app.services.user_scope import report_currency_for

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")

# Generous enough to bridge a normal weekend/holiday gap when resolving "the
# price/FX applicable to this date" — same 4-day staleness a live read
# tolerates (portfolio_calculator._PRICE_STALE_DAYS / fx_fetcher.
# _FX_STALE_DAYS) would be too tight for a backfill walking many calendar
# days at once, so this is deliberately wider.
_PRICE_LOOKBACK_DAYS = 10
_FX_LOOKBACK_DAYS = 10

PriceLookupFn = Callable[[str, date], "tuple[Decimal, date] | None"]


def historical_price(session: Session, key: str, as_of_date: date) -> tuple[Decimal, date] | None:
    """Latest captured close for `key` (already-normalized ticker/fund_code)
    with `trade_date <= as_of_date`, within `_PRICE_LOOKBACK_DAYS`."""
    row = session.execute(
        select(PriceSnapshot.close, PriceSnapshot.trade_date)
        .where(
            PriceSnapshot.ticker == key,
            PriceSnapshot.session_node == "close",
            PriceSnapshot.close.is_not(None),
            PriceSnapshot.trade_date <= as_of_date,
            PriceSnapshot.trade_date >= as_of_date - timedelta(days=_PRICE_LOOKBACK_DAYS),
        )
        .order_by(PriceSnapshot.trade_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    close, trade_date = row
    return (close, trade_date)


def historical_fx_rates_asof(
    session: Session, as_of_date: date, lookback_days: int = _FX_LOOKBACK_DAYS
) -> dict[str, tuple[Decimal, date]]:
    """Per-pair latest rate with `rate_date <= as_of_date`, each pair
    resolved independently within `lookback_days` — same per-pair
    independence issue #354 established for "latest", applied here to an
    arbitrary historical date instead of "today"."""
    latest_per_pair = (
        select(FxRate.pair, func.max(FxRate.rate_date).label("rate_date"))
        .where(
            FxRate.rate_date <= as_of_date,
            FxRate.rate_date >= as_of_date - timedelta(days=lookback_days),
        )
        .group_by(FxRate.pair)
        .subquery()
    )
    rows = session.execute(
        select(FxRate.pair, FxRate.rate, FxRate.rate_date).join(
            latest_per_pair,
            (FxRate.pair == latest_per_pair.c.pair)
            & (FxRate.rate_date == latest_per_pair.c.rate_date),
        )
    ).all()
    return {pair: (rate, rate_date) for pair, rate, rate_date in rows}


def required_fx_pairs(holdings: list[Holding], base_currency: str) -> set[str]:
    """FX pairs needed to price every holding's currency into base_currency."""
    pairs: set[str] = set()
    for h in holdings:
        if h.currency != base_currency:
            pair = CURRENCY_TO_FX_PAIR.get(h.currency)
            if pair:
                pairs.add(pair)
    if base_currency != "USD":
        pair = CURRENCY_TO_FX_PAIR.get(base_currency)
        if pair:
            pairs.add(pair)
    return pairs


@dataclass
class _LocalValue:
    value: Decimal | None
    shares: Decimal | None
    price_as_of: date | None
    data_quality: str  # "ok" | "insufficient"


def _local_value_for_holding(
    h: Holding, snapshot_date: date, price_lookup: PriceLookupFn
) -> _LocalValue:
    """Local-currency value of one holding as of `snapshot_date`, per D5.

    Cash/wmf and manual (incl. capture_supported=False) holdings: their
    stored `current_value` IS the local value — no "price return" concept,
    any change between two days is a cash flow, handled at the TWR-read
    layer, not here. Auto-priced holdings: shares x historical close.
    """
    if h.pricing_mode == "auto" and is_capture_supported(h):
        raw_key = h.ticker or h.fund_code or ""
        if not raw_key or h.shares is None:
            return _LocalValue(None, h.shares, None, "insufficient")
        priced = price_lookup(normalize_legacy_ticker(raw_key), snapshot_date)
        if priced is None:
            return _LocalValue(None, h.shares, None, "insufficient")
        price, trade_date = priced
        value = (price * h.shares).quantize(_CENT, rounding=ROUND_HALF_UP)
        return _LocalValue(value, h.shares, trade_date, "ok")
    # cash / wmf / manual / capture_supported=False: manual current_value is
    # the only usable local value.
    if h.current_value is None:
        return _LocalValue(None, None, None, "insufficient")
    return _LocalValue(h.current_value, None, None, "ok")


def build_snapshot_row(
    h: Holding,
    user_id: uuid.UUID,
    snapshot_date: date,
    base_currency: str,
    fx_rates: dict[str, tuple[Decimal, date]],
    price_lookup: PriceLookupFn,
    *,
    is_backfilled: bool,
    run_time_fx_rates: dict[str, Decimal] | None = None,
) -> dict[str, object]:
    """One `portfolio_value_snapshots` row's column values for one holding.

    `run_time_fx_rates` (D2/D6): when a historical FX rate can't be
    resolved for `snapshot_date` (backfill only), fall back to the script's
    own run-time current rate and flag `is_fx_fallback=True` — never done
    for the live daily task, which has no "current" fallback concept for a
    date that IS today.
    """
    local = _local_value_for_holding(h, snapshot_date, price_lookup)
    rates = {pair: rate for pair, (rate, _d) in fx_rates.items()}
    fx_dates = {pair: d for pair, (_r, d) in fx_rates.items()}

    market_value_base: Decimal | None = None
    fx_used: Decimal | None = None
    is_fx_fallback = False
    fx_as_of: date | None = None
    data_quality = local.data_quality

    if local.value is not None:
        converted = to_base(local.value, h.currency, base_currency, rates)
        rates_used_for_multiplier = rates
        if converted is None and is_backfilled and run_time_fx_rates is not None:
            converted = to_base(local.value, h.currency, base_currency, run_time_fx_rates)
            if converted is not None:
                is_fx_fallback = True
                rates_used_for_multiplier = run_time_fx_rates

        if converted is None:
            data_quality = "insufficient"
        else:
            market_value_base = converted.quantize(_CENT, rounding=ROUND_HALF_UP)
            fx_used = fx_multiplier(h.currency, base_currency, rates_used_for_multiplier)
            if not is_fx_fallback:
                pair = CURRENCY_TO_FX_PAIR.get(h.currency) or CURRENCY_TO_FX_PAIR.get(base_currency)
                fx_as_of = fx_dates.get(pair) if pair else None
            if is_fx_fallback:
                data_quality = "approx_fx"

    if is_backfilled and data_quality == "ok":
        data_quality = "approx_backfill"

    return {
        "user_id": user_id,
        "snapshot_date": snapshot_date,
        "holding_id": h.id,
        "ticker": h.ticker,
        "fund_code": h.fund_code,
        "market": h.market,
        "broker": h.broker,
        "account": h.account,
        "portfolio": h.portfolio,
        "asset_class": h.asset_class,
        "pricing_mode": h.pricing_mode,
        "capture_supported": is_capture_supported(h),
        "currency": h.currency,
        "shares": local.shares,
        "current_value": local.value if h.pricing_mode != "auto" else None,
        "market_value": local.value if h.pricing_mode == "auto" else None,
        "market_value_base": market_value_base,
        "cost_basis_base": None,
        "fx_rate_used": fx_used,
        "price_as_of": local.price_as_of,
        "fx_as_of": fx_as_of,
        "is_backfilled": is_backfilled,
        "is_fx_fallback": is_fx_fallback,
        "data_quality": data_quality,
    }


_UPSERT_UPDATE_COLUMNS = (
    "ticker",
    "fund_code",
    "market",
    "broker",
    "account",
    "portfolio",
    "asset_class",
    "pricing_mode",
    "capture_supported",
    "currency",
    "shares",
    "current_value",
    "market_value",
    "market_value_base",
    "cost_basis_base",
    "fx_rate_used",
    "price_as_of",
    "fx_as_of",
    "is_backfilled",
    "is_fx_fallback",
    "data_quality",
)


def _upsert_rows(session: Session, rows: list[dict[str, object]]) -> int:
    """Daily-task write mode: refresh an existing (user, date, holding) row
    in place — a same-day catch-up re-run must see the latest data, not be
    skipped as a no-op duplicate."""
    if not rows:
        return 0
    stmt = pg_insert(PortfolioValueSnapshot).values(rows)
    update_cols = {c: stmt.excluded[c] for c in _UPSERT_UPDATE_COLUMNS}
    result = stmt.on_conflict_do_update(
        constraint="uq_portfolio_value_snapshots_holding", set_=update_cols
    ).returning(PortfolioValueSnapshot.id)
    return len(session.execute(result).fetchall())


def _insert_rows_skip_existing(session: Session, rows: list[dict[str, object]]) -> int:
    """Backfill write mode: never overwrite an existing row (D2 amendment)."""
    if not rows:
        return 0
    stmt = (
        pg_insert(PortfolioValueSnapshot)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_portfolio_value_snapshots_holding")
        .returning(PortfolioValueSnapshot.id)
    )
    return len(session.execute(stmt).fetchall())


def get_or_create_batch(
    session: Session, user_id: uuid.UUID, snapshot_date: date
) -> PortfolioSnapshotBatch:
    batch = session.execute(
        select(PortfolioSnapshotBatch).where(
            PortfolioSnapshotBatch.user_id == user_id,
            PortfolioSnapshotBatch.snapshot_date == snapshot_date,
        )
    ).scalar_one_or_none()
    if batch is None:
        batch = PortfolioSnapshotBatch(
            user_id=user_id, snapshot_date=snapshot_date, status="pending"
        )
        session.add(batch)
        session.flush()
    return batch


def write_user_snapshot(
    session: Session,
    user_id: uuid.UUID,
    snapshot_date: date,
    *,
    is_backfilled: bool = False,
    upsert: bool = True,
    run_time_fx_rates: dict[str, Decimal] | None = None,
) -> tuple[int, str]:
    """Write every holding's row for one user/day. Returns (rows_written, batch_status).

    `upsert=True` (daily task / catch-up): refresh existing rows for the
    same key. `upsert=False` (backfill): never overwrite an existing row.

    Batch dependency check (design "Batch completeness"): if the user has
    at least one holding whose currency needs an FX pair this render can't
    resolve for `snapshot_date` at all, and no run-time fallback was
    supplied, the whole day is marked `skipped_deps` and NO rows are
    written — the daily task's next catch-up run covers it instead of
    silently writing a partial day.

    This gate is deliberately FX-only, not FX-AND-price (review 5124107298
    finding 3 flagged this asymmetry). A symmetric price-readiness check —
    "did today's price capture produce anything for this user's tickers
    yet" — was considered and rejected: this codebase has no real market
    holiday calendar (see `price_capture._session_lag`'s own admission that
    weekdays stand in for the XSHG session calendar), so a strict "at least
    one exact-date close must exist" check would misfire as `skipped_deps`
    on every US/HK/A-share holiday for a single-market book, silently
    losing legitimate trading-halt days from the chart — a worse failure
    mode than the one it would close. Per-holding price gaps already
    degrade gracefully to `data_quality="insufficient"` on that one row
    (see `_local_value_for_holding`) rather than blocking the whole batch,
    which is the accepted, documented tradeoff for Phase 1.
    """
    holdings = list(session.execute(select(Holding).where(Holding.user_id == user_id)).scalars())
    batch = get_or_create_batch(session, user_id, snapshot_date)
    if not holdings:
        batch.status = "complete"
        session.flush()
        return 0, batch.status

    base_currency = report_currency_for(session, user_id, "USD")
    fx_rates = historical_fx_rates_asof(session, snapshot_date)
    needed_pairs = required_fx_pairs(holdings, base_currency)
    missing_pairs = needed_pairs - set(fx_rates)

    if missing_pairs and (not is_backfilled or run_time_fx_rates is None):
        # Live daily path: FX capture hasn't finished for this date yet —
        # never write a partial day, retry on the next catch-up. Backfill
        # with no run-time fallback rates at all is a caller error (the
        # backfill script always supplies them, D2/D6) — same outcome.
        batch.status = "skipped_deps"
        session.flush()
        return 0, batch.status

    price_lookup: PriceLookupFn = partial(historical_price, session)
    rows = [
        build_snapshot_row(
            h,
            user_id,
            snapshot_date,
            base_currency,
            fx_rates,
            price_lookup,
            is_backfilled=is_backfilled,
            run_time_fx_rates=run_time_fx_rates if missing_pairs else None,
        )
        for h in holdings
    ]
    written = _upsert_rows(session, rows) if upsert else _insert_rows_skip_existing(session, rows)
    batch.status = "complete"
    session.flush()
    return written, batch.status


def capture_portfolio_value_snapshot(
    session: Session, snapshot_date: date | None = None
) -> dict[str, int]:
    """Daily beat entry point: write today's snapshot for every active user
    with at least one holding. Scheduled after the day's price-capture and
    FX-fetch tasks (see app/tasks/__init__.py) so `write_user_snapshot`'s
    dependency check almost always finds today's FX rate already there;
    when it doesn't (a delayed FX task), that user's day is marked
    `skipped_deps` and picked up by the next run rather than silently
    understating today's value.
    """
    target_date = snapshot_date or date.today()
    user_ids = list(
        session.execute(
            select(User.id).where(
                User.status == "active",
                User.id.in_(select(Holding.user_id).distinct()),
            )
        ).scalars()
    )
    written_total = 0
    complete = 0
    skipped = 0
    for user_id in user_ids:
        written, status = write_user_snapshot(session, user_id, target_date, is_backfilled=False)
        written_total += written
        if status == "complete":
            complete += 1
        else:
            skipped += 1
    logger.info(
        "capture_portfolio_value_snapshot: date=%s users=%d written=%d complete=%d skipped_deps=%d",
        target_date,
        len(user_ids),
        written_total,
        complete,
        skipped,
    )
    return {
        "users": len(user_ids),
        "written": written_total,
        "complete": complete,
        "skipped_deps": skipped,
    }
