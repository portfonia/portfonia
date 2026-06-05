"""Compute market values and base-currency totals for all holdings."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.fx_rate import FxRate
from app.models.holding import Holding

_ET = timezone(timedelta(hours=-5))  # EST; used for today's date boundary
_ZERO = Decimal("0")

# All FX rates are stored as 1 USD = X foreign. Build a lookup from
# currency code to that divisor for USD-base conversion.
_CURRENCY_TO_FX_PAIR: dict[str, str] = {
    "CNY": "USDCNY",
    "CNH": "USDCNH",
    "HKD": "USDHKD",
}


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
    market: str
    market_value: Decimal  # in holding's own currency
    market_value_base: Decimal  # in base_currency
    price_as_of: datetime | None
    is_stale: bool  # True if market_price missing for auto-mode


@dataclass
class PortfolioSnapshot:
    base_currency: str
    fx_date: date  # which day's FX rates were used
    holdings: list[HoldingValue] = field(default_factory=list)
    total_base: Decimal = _ZERO
    by_currency: dict[str, Decimal] = field(default_factory=dict)  # currency → total in base
    by_asset_type: dict[str, Decimal] = field(default_factory=dict)  # asset_type → total in base
    by_market: dict[str, Decimal] = field(default_factory=dict)  # market → total in base
    stale_tickers: list[str] = field(default_factory=list)  # tickers with no fresh price


def _load_fx_rates(session: Session) -> tuple[dict[str, Decimal], date]:
    """
    Return the most recent fx_rates snapshot as {pair: rate} plus the date used.

    Prefers today (ET). Falls back to the newest available date if today is
    missing (weekend, holiday). This keeps the calculator functional even when
    update_fx_rates() hasn't run yet today.
    """
    today_et = datetime.now(tz=_ET).date()

    # Try today first, then fall back to most recent.
    rows = (
        session.execute(
            select(FxRate)
            .where(FxRate.rate_date <= today_et)
            .order_by(FxRate.rate_date.desc())
            .limit(len(_CURRENCY_TO_FX_PAIR) * 2)  # enough for one full snapshot
        )
        .scalars()
        .all()
    )

    if not rows:
        return {}, today_et

    # Use the most recent date that has at least one rate.
    fx_date = rows[0].rate_date
    rates: dict[str, Decimal] = {}
    for row in rows:
        if row.rate_date == fx_date:
            rates[row.pair] = row.rate

    return rates, fx_date


def _to_base(
    amount: Decimal,
    currency: str,
    base_currency: str,
    fx: dict[str, Decimal],
) -> Decimal | None:
    """
    Convert `amount` in `currency` to `base_currency`.

    Returns None if the required FX rate is missing.
    All rates are 1 USD = X foreign, so:
      - to USD:  amount / fx["USD{currency}"]
      - to HKD:  (amount → USD) * fx["USDHKD"]
      - to CNY:  (amount → USD) * fx["USDCNY"]
    """
    if currency == base_currency:
        return amount

    # Step 1: convert to USD
    if currency == "USD":
        amount_usd = amount
    else:
        pair = _CURRENCY_TO_FX_PAIR.get(currency)
        if pair is None or pair not in fx:
            return None
        amount_usd = amount / fx[pair]

    if base_currency == "USD":
        return amount_usd

    # Step 2: USD → base_currency
    pair = _CURRENCY_TO_FX_PAIR.get(base_currency)
    if pair is None or pair not in fx:
        return None
    return amount_usd * fx[pair]


def compute_portfolio(
    session: Session,
    base_currency: str = "USD",
) -> PortfolioSnapshot:
    """
    Compute market values for all holdings and aggregate into a PortfolioSnapshot.

    FX rates are loaded once at the top and passed as a constant to all
    per-holding conversions (design doc §6.2: same as_of_date for all holdings).
    """
    fx, fx_date = _load_fx_rates(session)
    snapshot = PortfolioSnapshot(base_currency=base_currency, fx_date=fx_date)

    holdings: list[Holding] = list(session.execute(select(Holding)).scalars())

    for h in holdings:
        # --- compute market_value in holding's own currency ---
        if h.pricing_mode == "auto":
            if h.market_price is None or h.shares is None:
                snapshot.stale_tickers.append(h.ticker or h.fund_code or h.name)
                continue
            market_value = (h.market_price * h.shares).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        else:
            # manual: current_value is already total market value
            if h.current_value is None:
                snapshot.stale_tickers.append(h.name)
                continue
            market_value = h.current_value

        # --- convert to base currency ---
        market_value_base = _to_base(market_value, h.currency, base_currency, fx)
        if market_value_base is None:
            snapshot.stale_tickers.append(h.ticker or h.fund_code or h.name)
            continue
        market_value_base = market_value_base.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        market = _classify_market(h)

        hv = HoldingValue(
            holding_id=h.id,
            name=h.name,
            ticker=h.ticker,
            fund_code=h.fund_code,
            currency=h.currency,
            asset_type=h.asset_type,
            market=market,
            market_value=market_value,
            market_value_base=market_value_base,
            price_as_of=h.price_as_of,
            is_stale=False,
        )
        snapshot.holdings.append(hv)

        # --- accumulate aggregates ---
        snapshot.total_base += market_value_base

        snapshot.by_currency[h.currency] = (
            snapshot.by_currency.get(h.currency, _ZERO) + market_value_base
        )
        asset_key = h.asset_type or "other"
        snapshot.by_asset_type[asset_key] = (
            snapshot.by_asset_type.get(asset_key, _ZERO) + market_value_base
        )
        snapshot.by_market[market] = snapshot.by_market.get(market, _ZERO) + market_value_base

    return snapshot
