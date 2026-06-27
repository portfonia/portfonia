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
from app.services._yfinance import _normalize_hk_ticker
from app.services.asset_class_config import load_asset_class_config

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_RATIO = Decimal("0.0001")  # 4 dp for fractions (0..1)

# Calendar days beyond which a captured close is considered stale.
# Covers 3-day holiday weekends (e.g. Fri close → Tue report = 4 days);
# a 5+ day gap indicates a capture pipeline failure, not normal market closure.
_PRICE_STALE_DAYS = 4

# All FX rates are stored as 1 USD = X foreign.
_CURRENCY_TO_FX_PAIR: dict[str, str] = {
    "CNY": "USDCNY",
    "CNH": "USDCNH",
    "HKD": "USDHKD",
}

# §6.5 snapshot concentration thresholds — admin-editable, see
# config/asset_class_thresholds.yml (#35). Single-holding watch/high are
# differentiated by the TOP holding's own asset_class: a single individual
# stock concentrates idiosyncratic risk, while a broad index fund is already
# internally diversified, so the same weight carries different risk. Falls
# back to this flat default for any asset_class missing from the config
# (defensive only — the loader's validation should make that impossible).
_DEFAULT_SINGLE_THRESHOLD = (Decimal("0.15"), Decimal("0.25"))

# Asset types that carry a single equity sector (others excluded from §6.4 chart).
# Sector is retained only for forward-event holding relevance mapping
# (rate-sensitive / consumer sectors) — no longer used for §1/distribution/§4.1.
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
    asset_class: str | None
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
    top_holding_asset_class: str | None = None
    top3_ratio: Decimal | None = None  # top 3 holdings / total
    top_asset_class_name: str | None = None
    top_asset_class_ratio: Decimal | None = None  # largest asset_class bucket / total
    single_holding_watch: bool = False  # threshold depends on top_holding_asset_class
    single_holding_high: bool = False
    top3_watch: bool = False  # >50%
    asset_class_watch: bool = False  # >50%
    asset_class_high: bool = False  # >65%


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
    by_asset_class: dict[str, Decimal] = field(default_factory=dict)  # all holdings, §1/§4.1
    concentration: Concentration = field(default_factory=Concentration)
    stale_tickers: list[str] = field(default_factory=list)
    stale_priced_tickers: list[str] = field(default_factory=list)


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
    the same price series. Fund NAVs are stored under the fund_code key by
    capture_fund_navs(); the lookup in compute_portfolio uses h.ticker or h.fund_code.
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

    config = load_asset_class_config()
    ranked = sorted(snapshot.holdings, key=lambda h: h.market_value_base, reverse=True)

    top = ranked[0]
    c.top_holding_name = top.name
    c.top_holding_ratio = _ratio(top.market_value_base, total)
    c.top_holding_asset_class = top.asset_class
    top_thresholds = config.by_class.get(top.asset_class or "")
    watch, high = (
        (top_thresholds.concentration_watch, top_thresholds.concentration_high)
        if top_thresholds is not None
        else _DEFAULT_SINGLE_THRESHOLD
    )
    c.single_holding_watch = c.top_holding_ratio > watch
    c.single_holding_high = c.top_holding_ratio > high

    top3_sum = sum((h.market_value_base for h in ranked[:3]), _ZERO)
    c.top3_ratio = _ratio(top3_sum, total)
    c.top3_watch = c.top3_ratio > config.global_concentration.top3_watch

    if snapshot.by_asset_class:
        class_name, class_val = max(snapshot.by_asset_class.items(), key=lambda kv: kv[1])
        c.top_asset_class_name = class_name
        c.top_asset_class_ratio = _ratio(class_val, total)
        c.asset_class_watch = (
            c.top_asset_class_ratio > config.global_concentration.asset_class_bucket_watch
        )
        c.asset_class_high = (
            c.top_asset_class_ratio > config.global_concentration.asset_class_bucket_high
        )

    return c


def compute_portfolio(
    session: Session,
    user_id: uuid.UUID,
    base_currency: str = "USD",
    as_of: date | None = None,
) -> PortfolioSnapshot:
    """
    Compute market values for all holdings and aggregate into a snapshot.

    FX rates are loaded once and held constant across every conversion
    (design §6.2: one as_of_date per report). Holdings missing a price or an
    FX rate are recorded in `stale_tickers` and excluded from all totals.
    Holdings whose captured close is older than _PRICE_STALE_DAYS relative to
    `as_of` are recorded in `stale_priced_tickers` (included in totals but
    flagged for ops alerting).
    """
    price_ref = as_of or date.today()
    fx, fx_date = _load_fx_rates(session)
    snapshot = PortfolioSnapshot(base_currency=base_currency, fx_date=fx_date or date.today())
    captured_closes = _latest_captured_closes(session)

    holdings: list[Holding] = list(
        session.execute(select(Holding).where(Holding.user_id == user_id)).scalars()
    )

    for h in holdings:
        # --- market value in the holding's own currency ---
        if h.pricing_mode == "auto":
            # Prefer the capture-layer close so valuation and the anomaly baseline
            # share one price series; fall back to the /refresh market_price (e.g.
            # funds, which are not captured by ticker).
            price = h.market_price
            price_as_of = h.price_as_of
            # Fund NAVs are stored in price_snapshots under the fund_code key.
            raw_key = h.ticker or h.fund_code or ""
            captured = captured_closes.get(_normalize_hk_ticker(raw_key))
            if captured is not None:
                price, trade_date = captured
                price_as_of = datetime.combine(trade_date, datetime.min.time(), tzinfo=ET)
                if (price_ref - trade_date).days > _PRICE_STALE_DAYS:
                    snapshot.stale_priced_tickers.append(h.ticker or h.fund_code or h.name)
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
                asset_class=h.asset_class,
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

        # Every holding has an asset_class (server_default on the model), so
        # this bucket has no "Other" fallback and naturally merges the same
        # underlying exposure across markets (e.g. VOO + 513650.SS both
        # EQUITY_US_BROAD) — this is what §1/distribution/§4.1 read.
        class_key = h.asset_class
        snapshot.by_asset_class[class_key] = (
            snapshot.by_asset_class.get(class_key, _ZERO) + market_value_base
        )

        # Equity sectors only; funds/cash/wmf are shown via by_asset_type instead.
        # Retained for forward-event holding-relevance mapping only (rate-
        # sensitive / consumer sectors) — no longer read by §1/distribution/§4.1.
        if h.asset_type in _SECTOR_ASSET_TYPES:
            sector_key = h.sector or "Other"
            snapshot.by_sector[sector_key] = (
                snapshot.by_sector.get(sector_key, _ZERO) + market_value_base
            )

    snapshot.concentration = _compute_concentration(snapshot)
    return snapshot
