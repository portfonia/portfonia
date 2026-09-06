"""Pure currency-conversion helper shared by the Portfolio Performance
pipeline (issue #360 Phase 1).

Deliberately a small, standalone module rather than importing
`portfolio_calculator`'s private `_CURRENCY_TO_FX_PAIR`/`_to_base`: this
module's callers (portfolio_history.py, portfolio_performance.py,
benchmark_prices.py) need HISTORICAL, as-of-a-date conversion, not "latest
rate" — a different query shape entirely — and CLAUDE.md's task boundary
(never touch `/portfolio/summary`/`compute_portfolio`) makes refactoring
that private table out of portfolio_calculator.py out of scope for this
change. Small, self-contained duplication of a stable 14-entry mapping
mirrors this repo's existing per-module tiny-helper convention (see
CLAUDE.md's holdings_export.py/email_verification.py locale-helper note).
"""

from __future__ import annotations

from decimal import Decimal

# All FX rates in `fx_rates` are stored as 1 USD = X foreign — mirrors
# portfolio_calculator._CURRENCY_TO_FX_PAIR (issue #204) exactly. Keep this
# table in sync with that one and with fx_fetcher._PAIRS if a new currency
# is ever added to VALID_CURRENCIES.
CURRENCY_TO_FX_PAIR: dict[str, str] = {
    "CNY": "USDCNY",
    "CNH": "USDCNH",
    "HKD": "USDHKD",
    "GBP": "USDGBP",
    "EUR": "USDEUR",
    "JPY": "USDJPY",
    "SGD": "USDSGD",
    "AUD": "USDAUD",
    "CAD": "USDCAD",
    "CHF": "USDCHF",
    "KRW": "USDKRW",
    "TWD": "USDTWD",
    "MOP": "USDMOP",
    "NZD": "USDNZD",
}


def to_base(
    amount: Decimal,
    currency: str,
    base_currency: str,
    rates: dict[str, Decimal],
) -> Decimal | None:
    """Convert `amount` from `currency` to `base_currency` via a USD pivot.

    `rates` is {pair: rate} with rates stored as 1 USD = X foreign (same
    shape `portfolio_calculator._load_fx_rates` returns). Returns None if a
    required rate is missing — callers treat that as "insufficient", never
    as zero.
    """
    if currency == base_currency:
        return amount

    if currency == "USD":
        amount_usd = amount
    else:
        pair = CURRENCY_TO_FX_PAIR.get(currency)
        if pair is None or pair not in rates:
            return None
        amount_usd = amount / rates[pair]

    if base_currency == "USD":
        return amount_usd

    pair = CURRENCY_TO_FX_PAIR.get(base_currency)
    if pair is None or pair not in rates:
        return None
    return amount_usd * rates[pair]


def fx_multiplier(currency: str, base_currency: str, rates: dict[str, Decimal]) -> Decimal | None:
    """The single multiplier `m` such that `to_base(amount, ...) == amount * m`.

    Used where a caller needs to re-apply the same conversion to a
    different amount without re-deriving it (e.g. `portfolio_history.py`'s
    per-holding row writer, where `fx_rate_used` is stored so a later
    reader can recover the base-currency conversion without re-querying
    `fx_rates`). Returns None under the same conditions `to_base` would.
    """
    converted = to_base(Decimal("1"), currency, base_currency, rates)
    return converted
