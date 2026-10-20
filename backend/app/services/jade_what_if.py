"""Transient hypothetical holdings for the Jade comparison."""

from dataclasses import replace
from decimal import Decimal
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException

from app.schemas.jade import JadeWhatIfIn, WhatIfRowIn
from app.services.jade_replay_config import WHAT_IF_ETFS, WHAT_IF_SYMBOLS
from app.services.portfolio_calculator import HoldingValue


def validate_versions(body: JadeWhatIfIn, current: list[HoldingValue]) -> None:
    def reject(code: str) -> None:
        raise HTTPException(422, detail=code)

    if not 1 <= len(body.versions) <= 2:
        reject("invalid_versions")
    owned = {h.holding_id: h for h in current}
    for version in body.versions:
        if not version.rows:
            reject("empty_version")
        seen: set[object] = set()
        for row in version.rows:
            key = (
                row.kind,
                row.holding_id
                if row.kind == "holding"
                else row.symbol
                if row.kind == "etf"
                else None,
            )
            if row.kind == "holding" and row.holding_id not in owned:
                reject("holding_not_found")
            if row.kind == "etf" and row.symbol not in WHAT_IF_SYMBOLS:
                reject("symbol_not_allowed")
            if key in seen:
                reject("duplicate_row")
            seen.add(key)
        for row in version.rows:
            if (
                not row.amount.is_finite()
                or row.amount < 0
                or cast(int, row.amount.as_tuple().exponent) < -2
            ):
                reject("invalid_amount")
        for row in version.rows:
            if row.kind == "holding":
                assert row.holding_id is not None
                value = owned[row.holding_id].market_value_base
                if value is None or value <= 0:
                    reject("holding_unvalued")
        if sum((r.amount for r in version.rows), Decimal(0)) <= 0:
            reject("empty_version")


def what_if_holdings(
    current: list[HoldingValue], rows: list[WhatIfRowIn], base_currency: str
) -> list[HoldingValue]:
    owned = {h.holding_id: h for h in current}
    etfs = {e.spec.symbol: e for e in WHAT_IF_ETFS}
    out = []
    for row in rows:
        if row.amount == 0:
            continue
        if row.kind == "holding":
            assert row.holding_id is not None
            out.append(replace(owned[row.holding_id], market_value_base=row.amount))
            continue
        etf = etfs[row.symbol] if row.kind == "etf" and row.symbol is not None else None
        symbol = etf.spec.symbol if etf else "cash"
        out.append(
            HoldingValue(
                holding_id=uuid5(NAMESPACE_URL, "portfonia:jade-what-if:" + symbol),
                name=etf.spec.name if etf else "Cash",
                ticker=etf.spec.symbol if etf else None,
                fund_code=None,
                currency=etf.spec.currency if etf else base_currency,
                asset_type="etf" if etf else "cash",
                asset_class=etf.asset_class if etf else "CASH_EQUIV",
                market=etf.market if etf else "Other",
                market_value=None,
                market_value_base=row.amount,
                price_as_of=None,
            )
        )
    return out
