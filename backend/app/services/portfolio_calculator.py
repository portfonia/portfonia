"""Compute market values, distributions, and concentration for all holdings."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_RATIO = Decimal("0.0001")  # 4 dp for fractions (0..1)

# All FX rates are stored as 1 USD = X foreign.
_CURRENCY_TO_FX_PAIR: dict[str, str] = {
    "CNY": "USDCNY",
    "CNH": "USDCNH",
    "HKD": "USDHKD",
}

# §6.5 snapshot concentration thresholds (fractions of total portfolio).
_SINGLE_WATCH = Decimal("0.15")
_SINGLE_HIGH = Decimal("0.25")
_TOP3_WATCH = Decimal("0.50")
_SECTOR_WATCH = Decimal("0.35")

# Asset types that carry a single equity sector (others excluded from §6.4 chart).
_SECTOR_ASSET_TYPES = {"stock", "etf"}


def _classify_market(holding: Holding) -> str:
    """Infer the exchange/market for a holding from ticker suffix or fund_code."""
    ticker = holding.ticker or ""
    if ticker.endswith(".HK"):
        return "HK"
    if ticker.endswith(".SS") or ticker.endswith(".SZ"):
        return "A-Share"
    if holding.fund_code:
        return "A-Share"
    if holding.asset_type in ("cash", "wmf"):
        return "Other"
    if ticker:
        return "US"
    return "Other"


@dataclass
class HoldingValue:
    holding_id: uuid.UUID
    name: str
    ticker: str | None
    fund_code: str | None
    currency: str
    asset_type: str | None
    sector: str | None
    market: str
    market_value: Decimal  # in holding's own currency
    market_value_base: Decimal  # in base_currency
    price_as_of: datetime | None
    broker: str | None = None  # custodian / holding institution, for §1 grouping
    position: int | None = None  # upload order, for report layout


@dataclass
class Concentration:
    """Raw §6.5 concentration figures + threshold breach flags.

    Numbers only — no prose. The advisory-sounding language templates in §6.5
    ("参考上限 25%") belong to the report layer, not here.
    """

    top_holding_name: str | None = None
    top_holding_ratio: Decimal | None = None  # largest single holding / total
    top3_ratio: Decimal | None = None  # top 3 holdings / total
    top_sector_name: str | None = None
    top_sector_ratio: Decimal | None = None  # largest equity sector / total
    single_holding_watch: bool = False  # >15%
    single_holding_high: bool = False  # >25%
    top3_watch: bool = False  # >50%
    sector_watch: bool = False  # >35%


@dataclass
class PortfolioSnapshot:
    base_currency: str
    fx_date: date  # which day's FX rates were used
    holdings: list[HoldingValue] = field(default_factory=list)
    total_base: Decimal = _ZERO
    by_currency: dict[str, Decimal] = field(default_factory=dict)
    by_asset_type: dict[str, Decimal] = field(default_factory=dict)
    by_market: dict[str, Decimal] = field(default_factory=dict)
    by_sector: dict[str, Decimal] = field(default_factory=dict)  # equity sectors only, §6.4
    concentration: Concentration = field(default_factory=Concentration)
    stale_tickers: list[str] = field(default_factory=list)


def _load_fx_rates(session: Session) -> tuple[dict[str, Decimal], date | None]:
    """
    Return the most recent fx_rates snapshot as {pair: rate} plus its date.

    Picks the newest rate_date on or before today (ET), then loads every pair
    for exactly that date. Falls back to the latest available date on
    weekends / holidays. Returns ({}, None) when the table is empty.
    """
    today_et = datetime.now(tz=ET).date()

    latest_date = session.execute(
        select(func.max(FxRate.rate_date)).where(FxRate.rate_date <= today_et)
    ).scalar_one_or_none()

    if latest_date is None:
        return {}, None

    rows = session.execute(select(FxRate).where(FxRate.rate_date == latest_date)).scalars()
    rates = {row.pair: row.rate for row in rows}
    return rates, latest_date


def _latest_captured_closes(session: Session) -> dict[str, tuple[Decimal, date]]:
    """Latest captured daily close per ticker, as {ticker: (close, trade_date)}.

    Valuation reads from the capture layer so §1 and the anomaly baseline agree on
    the same price series, instead of the ad-hoc ``holding.market_price`` written
    by the manual /refresh path. Funds (fund_code, no ticker) are not captured
    here and keep their NAV-derived ``market_price``.
    """
    latest_dates = (
        select(
            PriceSnapshot.ticker.label("ticker"),
            func.max(PriceSnapshot.trade_date).label("d"),
        )
        .where(PriceSnapshot.session_node == "close", PriceSnapshot.close.is_not(None))
        .group_by(PriceSnapshot.ticker)
        .subquery()
    )
    rows = session.execute(
        select(PriceSnapshot.ticker, PriceSnapshot.close, PriceSnapshot.trade_date).join(
            latest_dates,
            (PriceSnapshot.ticker == latest_dates.c.ticker)
            & (PriceSnapshot.trade_date == latest_dates.c.d)
            & (PriceSnapshot.session_node == "close"),
        )
    ).all()
    out: dict[str, tuple[Decimal, date]] = {}
    for ticker, close, trade_date in rows:
        if close is not None:
            out[ticker] = (close, trade_date)
    return out


def _to_base(
    amount: Decimal,
    currency: str,
    base_currency: str,
    fx: dict[str, Decimal],
) -> Decimal | None:
    """
    Convert `amount` from `currency` to `base_currency` via a USD pivot.

    Rates are 1 USD = X foreign. Returns None if a required rate is missing.
    """
    if currency == base_currency:
        return amount

    # Step 1: → USD
    if currency == "USD":
        amount_usd = amount
    else:
        pair = _CURRENCY_TO_FX_PAIR.get(currency)
        if pair is None or pair not in fx:
            return None
        amount_usd = amount / fx[pair]

    if base_currency == "USD":
        return amount_usd

    # Step 2: USD → base
    pair = _CURRENCY_TO_FX_PAIR.get(base_currency)
    if pair is None or pair not in fx:
        return None
    return amount_usd * fx[pair]


def _ratio(part: Decimal, whole: Decimal) -> Decimal:
    return (part / whole).quantize(_RATIO, rounding=ROUND_HALF_UP)


def _compute_concentration(snapshot: PortfolioSnapshot) -> Concentration:
    """Derive §6.5 snapshot concentration from already-aggregated values."""
    c = Concentration()
    total = snapshot.total_base
    if total <= _ZERO or not snapshot.holdings:
        return c

    ranked = sorted(snapshot.holdings, key=lambda h: h.market_value_base, reverse=True)

    top = ranked[0]
    c.top_holding_name = top.name
    c.top_holding_ratio = _ratio(top.market_value_base, total)
    c.single_holding_watch = c.top_holding_ratio > _SINGLE_WATCH
    c.single_holding_high = c.top_holding_ratio > _SINGLE_HIGH

    top3_sum = sum((h.market_value_base for h in ranked[:3]), _ZERO)
    c.top3_ratio = _ratio(top3_sum, total)
    c.top3_watch = c.top3_ratio > _TOP3_WATCH

    if snapshot.by_sector:
        sector_name, sector_val = max(snapshot.by_sector.items(), key=lambda kv: kv[1])
        c.top_sector_name = sector_name
        c.top_sector_ratio = _ratio(sector_val, total)
        c.sector_watch = c.top_sector_ratio > _SECTOR_WATCH

    return c


def compute_portfolio(
    session: Session,
    base_currency: str = "USD",
) -> PortfolioSnapshot:
    """
    Compute market values for all holdings and aggregate into a snapshot.

    FX rates are loaded once and held constant across every conversion
    (design §6.2: one as_of_date per report). Holdings missing a price or an
    FX rate are recorded in `stale_tickers` and excluded from all totals.
    """
    fx, fx_date = _load_fx_rates(session)
    snapshot = PortfolioSnapshot(base_currency=base_currency, fx_date=fx_date or date.today())
    captured_closes = _latest_captured_closes(session)

    # D-DEBT-2 (single-user only): no user_id filter — this loads ALL holdings.
    # Safe under Ring 0's single user; MUST add `.where(Holding.user_id == ...)`
    # before any multi-user deployment or it will mix tenants into one report.
    holdings: list[Holding] = list(session.execute(select(Holding)).scalars())

    for h in holdings:
        # --- market value in the holding's own currency ---
        if h.pricing_mode == "auto":
            # Prefer the capture-layer close so valuation and the anomaly baseline
            # share one price series; fall back to the /refresh market_price (e.g.
            # funds, which are not captured by ticker).
            price = h.market_price
            price_as_of = h.price_as_of
            captured = captured_closes.get(h.ticker) if h.ticker else None
            if captured is not None:
                price, trade_date = captured
                price_as_of = datetime.combine(trade_date, datetime.min.time(), tzinfo=ET)
            if price is None or h.shares is None:
                snapshot.stale_tickers.append(h.ticker or h.fund_code or h.name)
                continue
            market_value = (price * h.shares).quantize(_CENT, rounding=ROUND_HALF_UP)
        else:
            if h.current_value is None:
                snapshot.stale_tickers.append(h.name)
                continue
            market_value = h.current_value

        # --- convert to base currency ---
        converted = _to_base(market_value, h.currency, base_currency, fx)
        if converted is None:
            snapshot.stale_tickers.append(h.ticker or h.fund_code or h.name)
            continue
        market_value_base = converted.quantize(_CENT, rounding=ROUND_HALF_UP)

        # Prefer the user-declared market; derive from ticker only when absent.
        market = h.market or _classify_market(h)
        snapshot.holdings.append(
            HoldingValue(
                holding_id=h.id,
                name=h.name,
                ticker=h.ticker,
                fund_code=h.fund_code,
                currency=h.currency,
                asset_type=h.asset_type,
                sector=h.sector,
                market=market,
                market_value=market_value,
                market_value_base=market_value_base,
                price_as_of=price_as_of if h.pricing_mode == "auto" else h.price_as_of,
                broker=h.broker,
                position=h.position,
            )
        )

        # --- aggregates ---
        snapshot.total_base += market_value_base
        snapshot.by_currency[h.currency] = (
            snapshot.by_currency.get(h.currency, _ZERO) + market_value_base
        )
        asset_key = h.asset_type or "other"
        snapshot.by_asset_type[asset_key] = (
            snapshot.by_asset_type.get(asset_key, _ZERO) + market_value_base
        )
        snapshot.by_market[market] = snapshot.by_market.get(market, _ZERO) + market_value_base

        # Equity sectors only; funds/cash/wmf are shown via by_asset_type instead.
        if h.asset_type in _SECTOR_ASSET_TYPES:
            sector_key = h.sector or "Other"
            snapshot.by_sector[sector_key] = (
                snapshot.by_sector.get(sector_key, _ZERO) + market_value_base
            )

    snapshot.concentration = _compute_concentration(snapshot)
    return snapshot
