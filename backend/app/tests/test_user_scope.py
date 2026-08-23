"""Tests for user_scope.py (issue #128 A1) — the Stage-A user/identifier
universe helpers `active_user_ids`, `user_holdings`, `global_identifier_universe`.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.user import User
from app.services.user_scope import active_user_ids, global_identifier_universe, user_holdings

_U1 = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_U2 = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _h(**kwargs: object) -> Holding:
    defaults: dict[str, object] = {"pricing_mode": "auto", "currency": "USD"}
    return Holding(**{**defaults, **kwargs})


# --- active_user_ids ----------------------------------------------------------


def _user(user_id: uuid.UUID, email: str) -> User:
    return User(
        id=user_id,
        auth_provider="supabase",
        auth_subject=f"sub-{user_id}",
        email=email,
        status="active",
        locale="zh",
        base_currency="USD",
        report_cadence="mwf",
    )


def test_active_user_ids_empty_when_no_users(db_session: Session) -> None:
    assert active_user_ids(db_session) == []


def test_active_user_ids_returns_distinct_sorted_users(db_session: Session) -> None:
    db_session.add_all(
        [
            _user(_U2, "u2@example.com"),
            _user(_U1, "u1@example.com"),
            _h(user_id=_U1, name="NVIDIA", ticker="NVDA"),
        ]
    )
    db_session.flush()
    assert active_user_ids(db_session) == sorted([_U1, _U2])


# --- user_holdings --------------------------------------------------------------


def test_user_holdings_scoped_to_one_user(db_session: Session) -> None:
    db_session.add_all(
        [
            _h(user_id=_U1, name="NVIDIA", ticker="NVDA"),
            _h(user_id=_U2, name="Apple", ticker="AAPL"),
        ]
    )
    db_session.flush()
    rows = user_holdings(db_session, _U1)
    assert [h.ticker for h in rows] == ["NVDA"]


def test_user_holdings_empty_for_unknown_user(db_session: Session) -> None:
    db_session.add(_h(user_id=_U1, name="NVIDIA", ticker="NVDA"))
    db_session.flush()
    assert user_holdings(db_session, uuid.uuid4()) == []


# --- global_identifier_universe --------------------------------------------------


def test_global_identifier_universe_groups_shared_identifier_across_users(
    db_session: Session,
) -> None:
    """The whole point of L0: two users' rows for the SAME identifier land
    under one key, not two — this is what lets compute_global_moves query it
    once instead of once per holding row."""
    db_session.add_all(
        [
            _h(user_id=_U1, name="NVIDIA", ticker="NVDA"),
            _h(user_id=_U2, name="NVIDIA", ticker="NVDA"),
        ]
    )
    db_session.flush()
    universe = global_identifier_universe(db_session)
    assert set(universe.keys()) == {"NVDA"}
    assert {h.user_id for h in universe["NVDA"]} == {_U1, _U2}


def test_global_identifier_universe_excludes_manual_pricing_mode(db_session: Session) -> None:
    """A manually-priced holding has no quotable series for L0 to compute
    over — matches the pre-A1 detect_window_anomalies filter."""
    db_session.add_all(
        [
            _h(user_id=_U1, name="NVIDIA", ticker="NVDA"),
            _h(user_id=_U1, name="Cash-ish", ticker="MANUAL", pricing_mode="manual"),
        ]
    )
    db_session.flush()
    assert set(global_identifier_universe(db_session).keys()) == {"NVDA"}


def test_global_identifier_universe_excludes_holdings_with_no_ticker_or_fund_code(
    db_session: Session,
) -> None:
    db_session.add(_h(user_id=_U1, name="Cash", pricing_mode="manual"))
    db_session.flush()
    assert global_identifier_universe(db_session) == {}


def test_global_identifier_universe_normalizes_hk_tickers(db_session: Session) -> None:
    """Two users entering the same HK name in different raw forms (short vs
    zero-padded) must land under the SAME normalized key — otherwise L0
    would silently compute two separate move series for one real identifier."""
    db_session.add_all(
        [
            _h(user_id=_U1, name="Tencent", ticker="700.HK", currency="HKD"),
            _h(user_id=_U2, name="Tencent", ticker="0700.HK", currency="HKD"),
        ]
    )
    db_session.flush()
    universe = global_identifier_universe(db_session)
    assert set(universe.keys()) == {"0700.HK"}
    assert len(universe["0700.HK"]) == 2


def test_global_identifier_universe_uses_fund_code_when_no_ticker(db_session: Session) -> None:
    db_session.add(_h(user_id=_U1, name="Gold Fund", fund_code="019547", currency="CNY"))
    db_session.flush()
    assert set(global_identifier_universe(db_session).keys()) == {"019547"}
