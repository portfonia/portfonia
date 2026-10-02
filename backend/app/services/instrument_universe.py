"""Public instrument identifiers across all users, with capture's market rule."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.services.instrument_symbols import InstrumentKey, intelligence_identifier
from app.services.markets import is_capture_supported
from app.services.price_capture import _effective_market


@dataclass(frozen=True)
class UniverseEntry:
    identifier: str
    ticker: str
    market: str


def intel_universe(session: Session, market: str | None = None) -> list[UniverseEntry]:
    entries = {}
    for h in session.scalars(
        select(Holding).where(
            Holding.ticker.is_not(None),
            Holding.pricing_mode == "auto",
            Holding.asset_type == "stock",
        )
    ):
        effective = _effective_market(h)
        if h.ticker and is_capture_supported(h) and (market is None or effective == market):
            ident = intelligence_identifier(InstrumentKey("ticker", h.ticker))
            entries[ident] = UniverseEntry(ident, ident, effective)
    return sorted(entries.values(), key=lambda e: e.identifier)
