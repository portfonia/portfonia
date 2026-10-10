"""Issue #720 follow-up: watch-only labeling leaves calculations unchanged."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.services import jade_replay, jade_stress
from app.services.jade_replay_config import SCENARIOS
from app.services.portfolio_calculator import compute_portfolio
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_jade_replay import E, S, calendar, holding, series
from app.tests.test_jade_stress import cached


@pytest.mark.parametrize(
    "shares,watch_tier,method,reason",
    [
        ("0", "critical", "excluded", "watch_only"),
        ("0", None, "excluded", "unvalued"),
        ("1", "critical", "own", None),
    ],
)
def test_replay_watch_only(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    shares: str,
    watch_tier: str | None,
    method: str,
    reason: str | None,
) -> None:
    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr(jade_replay, "today_et", lambda: E)
    days = [E - timedelta(days=i) for i in reversed(range(12))]
    calendar(db_session, days)
    series(db_session, "yf:AAPL", [S, *days], [100.0] * 13)
    series(db_session, "yf:SPY", [S, *days], [100.0] * 13)
    h = holding(db_session, auto=True)
    h.shares = Decimal(shares)
    db_session.flush()
    before = jade_replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y")
    h.watch_tier = watch_tier
    db_session.flush()
    out = jade_replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y")
    row = out.holdings[0]
    assert (row.method, row.excluded_reason) == (method, reason)
    assert row.weight == ("1.000000" if shares == "1" else None)
    assert out.status == ("ok" if shares == "1" else "no_holdings")
    assert out.model_dump(exclude={"holdings"}) == before.model_dump(exclude={"holdings"})


@pytest.mark.parametrize(
    "shares,watch_tier,method,reason",
    [
        ("0", "critical", "excluded", "watch_only"),
        ("0", None, "excluded", "unvalued"),
        ("1", "critical", "own", None),
    ],
)
def test_stress_watch_only_all_scenarios(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    shares: str,
    watch_tier: str | None,
    method: str,
    reason: str | None,
) -> None:
    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr(jade_stress, "today_et", lambda: E)
    for spec in SCENARIOS:
        days = [
            spec.start,
            *[spec.peak - timedelta(days=i) for i in reversed(range(5))],
            *[spec.trough + timedelta(days=i) for i in range(6)],
        ]
        for key in ("yf:AAPL", "yf:SPY"):
            cached(db_session, key, days, [100.0] * len(days), sid=spec.id)
    h = holding(db_session, auto=True)
    h.shares = Decimal(shares)
    db_session.flush()
    before = jade_stress.compute_stress(db_session, TEST_USER_ID, "USD", "sp500")
    h.watch_tier = watch_tier
    db_session.flush()
    out = jade_stress.compute_stress(db_session, TEST_USER_ID, "USD", "sp500")
    assert [scenario.id for scenario in out.scenarios] == [s.id for s in SCENARIOS]
    for original, scenario in zip(before.scenarios, out.scenarios, strict=True):
        row = scenario.holdings[0]
        assert (row.method, row.excluded_reason) == (method, reason)
        assert row.weight == ("1.000000" if shares == "1" else None)
        assert scenario.status == ("ok" if shares == "1" else "no_holdings")
        assert scenario.model_dump(exclude={"holdings"}) == original.model_dump(
            exclude={"holdings"}
        )


def test_replay_watched_positive_without_price(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr(jade_replay, "today_et", lambda: E)
    calendar(db_session, [E])
    h = holding(db_session, auto=True)
    h.watch_tier = "critical"
    h.shares = Decimal(1)
    h.market_price = None
    db_session.flush()
    snap = compute_portfolio(db_session, TEST_USER_ID, "USD")
    assert snap.holdings[0].market_value_base is None
    out = jade_replay.compute_replay(db_session, TEST_USER_ID, "USD", "sp500", "5Y")
    row = out.holdings[0]
    assert (row.method, row.excluded_reason) == ("excluded", "unvalued")


@pytest.mark.parametrize("scenario_id", [s.id for s in SCENARIOS])
def test_stress_watched_positive_without_price(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, scenario_id: str
) -> None:
    seed_user(db_session, TEST_USER_ID)
    monkeypatch.setattr(jade_stress, "today_et", lambda: E)
    h = holding(db_session, auto=True)
    h.watch_tier = "critical"
    h.shares = Decimal(1)
    h.market_price = None
    db_session.flush()
    snap = compute_portfolio(db_session, TEST_USER_ID, "USD")
    assert snap.holdings[0].market_value_base is None
    out = jade_stress.compute_stress(db_session, TEST_USER_ID, "USD", "sp500")
    scenario = next(s for s in out.scenarios if s.id == scenario_id)
    row = scenario.holdings[0]
    assert (row.method, row.excluded_reason) == ("excluded", "unvalued")
