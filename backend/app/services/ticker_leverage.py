"""System-wide leveraged-product multiplier CRUD + read-time lookup (issue #87).

``ticker_leverage_overrides`` is a system-wide table (not per-user), same
sharing model as ``ticker_themes``. Every write here normalizes the ticker
through the same helper the FX-pair/asset_class lookups use
(``_normalize_ticker``) so a caller passing an un-normalized ticker (e.g.
``psh`` vs. ``PSH.L``) can never silently create a second row for what
should be one override — see the issue #204 mechanism note this mirrors.

Nothing here touches ``Holding.asset_class`` or any LLM parsing path;
``leverage_multiple`` is looked up, never parsed or inferred.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ticker_leverage import TickerLeverageOverride
from app.services._yfinance import _normalize_ticker


def normalize_leverage_ticker(ticker: str) -> str:
    """The one normalization path for this table's PK — mirrors the
    identifier normalization window_data.py/portfolio_calculator.py apply
    on the read side, so a lookup key built either way always matches."""
    return _normalize_ticker(ticker).upper()


@dataclass(frozen=True)
class LeverageOverride:
    ticker: str
    leverage_multiple: Decimal
    direction: str | None
    notes: str | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


def _to_dataclass(row: TickerLeverageOverride) -> LeverageOverride:
    return LeverageOverride(
        ticker=row.ticker,
        leverage_multiple=row.leverage_multiple,
        direction=row.direction,
        notes=row.notes,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class LeverageOverrideAlreadyExists(Exception):
    """Raised on create when the normalized ticker already has a row."""


class _Unset:
    """Sentinel distinguishing "field not supplied" from "field set to
    None" in update_leverage_override's kwargs — plain None already means
    "clear this field", so a default of None cannot also mean "leave
    unchanged"."""


_UNSET = _Unset()


def create_leverage_override(
    session: Session,
    *,
    ticker: str,
    leverage_multiple: Decimal,
    created_by: uuid.UUID,
    direction: str | None = None,
    notes: str | None = None,
) -> LeverageOverride:
    normalized = normalize_leverage_ticker(ticker)
    if session.get(TickerLeverageOverride, normalized) is not None:
        raise LeverageOverrideAlreadyExists(normalized)
    row = TickerLeverageOverride(
        ticker=normalized,
        leverage_multiple=leverage_multiple,
        direction=direction,
        notes=notes,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    return _to_dataclass(row)


def get_leverage_override(session: Session, ticker: str) -> LeverageOverride | None:
    row = session.get(TickerLeverageOverride, normalize_leverage_ticker(ticker))
    return _to_dataclass(row) if row is not None else None


def list_leverage_overrides(session: Session) -> list[LeverageOverride]:
    rows = (
        session.execute(select(TickerLeverageOverride).order_by(TickerLeverageOverride.ticker))
        .scalars()
        .all()
    )
    return [_to_dataclass(r) for r in rows]


def update_leverage_override(
    session: Session,
    ticker: str,
    *,
    leverage_multiple: Decimal | None = None,
    direction: str | None | _Unset = _UNSET,
    notes: str | None | _Unset = _UNSET,
) -> LeverageOverride:
    """Partial update. ``direction``/``notes`` default to the ``_UNSET``
    sentinel (leave unchanged) so a caller can explicitly clear either to
    None without that being indistinguishable from "not supplied" — same
    problem HoldingPatch's `exclude_unset` solves at the router layer,
    handled here since this function takes plain kwargs, not a Pydantic
    model."""
    row = session.get(TickerLeverageOverride, normalize_leverage_ticker(ticker))
    if row is None:
        raise LookupError("ticker leverage override not found")
    if leverage_multiple is not None:
        row.leverage_multiple = leverage_multiple
    if not isinstance(direction, _Unset):
        row.direction = direction
    if not isinstance(notes, _Unset):
        row.notes = notes
    session.flush()
    return _to_dataclass(row)


def delete_leverage_override(session: Session, ticker: str) -> None:
    row = session.get(TickerLeverageOverride, normalize_leverage_ticker(ticker))
    if row is None:
        raise LookupError("ticker leverage override not found")
    session.delete(row)
    session.flush()


def load_leverage_map(session: Session) -> dict[str, Decimal]:
    """normalized ticker -> leverage_multiple, for window_data.py /
    portfolio_calculator.py. Whole table, no cache (mirrors
    load_asset_class_config's no-cache convention) — this table is small
    and an admin edit should take effect on the very next report."""
    rows = session.execute(
        select(TickerLeverageOverride.ticker, TickerLeverageOverride.leverage_multiple)
    ).all()
    return {ticker: multiple for ticker, multiple in rows}
