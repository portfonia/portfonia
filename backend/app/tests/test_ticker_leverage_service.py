"""Unit/integration tests for app.services.ticker_leverage (issue #87).

Covers the two functions the admin router tests don't exercise directly:
normalize_leverage_ticker (the PK normalization contract window_data.py and
portfolio_calculator.py both rely on) and load_leverage_map (the bulk
read-time lookup those two modules call).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.ticker_leverage import TickerLeverageOverride
from app.services.ticker_leverage import (
    delete_leverage_override,
    load_leverage_map,
    normalize_leverage_ticker,
    update_leverage_override,
)

_ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000ad")


def test_normalize_leverage_ticker_uppercases_and_applies_collision_override() -> None:
    """Same normalization path as the FX-pair/asset_class lookups (issue
    #204) — a lowercase or known-collision ticker must resolve to the exact
    form the anomaly/concentration read-side identifiers use."""
    assert normalize_leverage_ticker("muu") == "MUU"
    assert normalize_leverage_ticker("psh") == "PSH.L"  # _TICKER_SYMBOL_OVERRIDE


def test_load_leverage_map_keys_by_normalized_ticker(db_session: Session) -> None:
    db_session.add(
        TickerLeverageOverride(ticker="MUU", leverage_multiple=Decimal("2"), created_by=_ADMIN)
    )
    db_session.add(
        TickerLeverageOverride(ticker="SOXL", leverage_multiple=Decimal("3"), created_by=_ADMIN)
    )
    db_session.flush()

    result = load_leverage_map(db_session)

    assert result == {"MUU": Decimal("2"), "SOXL": Decimal("3")}


def test_load_leverage_map_empty_table(db_session: Session) -> None:
    assert load_leverage_map(db_session) == {}


def test_update_leverage_override_unset_kwargs_leave_fields_untouched(db_session: Session) -> None:
    db_session.add(
        TickerLeverageOverride(
            ticker="MUU",
            leverage_multiple=Decimal("2"),
            direction="bull",
            notes="Direxion 2x MU",
            created_by=_ADMIN,
        )
    )
    db_session.flush()

    updated = update_leverage_override(db_session, "muu", leverage_multiple=Decimal("2.5"))

    assert updated.leverage_multiple == Decimal("2.5")
    assert updated.direction == "bull"  # not passed -> unchanged
    assert updated.notes == "Direxion 2x MU"  # not passed -> unchanged


def test_update_leverage_override_can_clear_optional_field_to_none(db_session: Session) -> None:
    db_session.add(
        TickerLeverageOverride(
            ticker="MUU", leverage_multiple=Decimal("2"), notes="x", created_by=_ADMIN
        )
    )
    db_session.flush()

    updated = update_leverage_override(db_session, "MUU", notes=None)

    assert updated.notes is None


def test_update_leverage_override_unknown_ticker_raises(db_session: Session) -> None:
    try:
        update_leverage_override(db_session, "NOPE", leverage_multiple=Decimal("2"))
        raise AssertionError("expected LookupError")
    except LookupError:
        pass


def test_delete_leverage_override_normalizes_lookup(db_session: Session) -> None:
    db_session.add(
        TickerLeverageOverride(ticker="MUU", leverage_multiple=Decimal("2"), created_by=_ADMIN)
    )
    db_session.flush()

    delete_leverage_override(db_session, "muu")

    db_session.expire_all()
    assert db_session.get(TickerLeverageOverride, "MUU") is None
