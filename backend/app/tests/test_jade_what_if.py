"""Issue #799 focused acceptance tests."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.services import jade_replay, jade_stress
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_jade_replay import calendar, holding, series


def test_b1_rebalanced_path() -> None:
    days = [date(2026, 1, 30), date(2026, 1, 31), date(2026, 2, 2), date(2026, 2, 3)]
    curves = [
        (dict(zip(days, map(Decimal, prices), strict=True)), Decimal(".5"))
        for prices in [("100", "110", "110", "121"), ("100", "100", "90", "90")]
    ]
    assert [v for _, v in jade_stress.rebalanced_path(days, curves, "monthly")] == pytest.approx(
        [1, 1.05, 1, 1.05]
    )
    assert [v for _, v in jade_stress.rebalanced_path(days, curves, "quarterly")] == pytest.approx(
        [1, 1.05, 1, 1.055]
    )
    quarters = [date(2026, 3, 30), date(2026, 3, 31), date(2026, 4, 1), date(2026, 4, 2)]
    curves = [
        (dict(zip(quarters, values.values(), strict=True)), weight) for values, weight in curves
    ]
    assert [
        v for _, v in jade_stress.rebalanced_path(quarters, curves, "quarterly")
    ] == pytest.approx([1, 1.05, 1, 1.05])


@pytest.fixture
def snapshot(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_user(db_session, TEST_USER_ID)
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_status = "active"
    user.subscription_type = "jade"
    days = [date(2026, 9, i) for i in range(1, 23)]
    calendar(db_session, days)
    series(
        db_session, "yf:SPY", [date(2025, 1, 1), *days], [100, *[100 + i for i in range(len(days))]]
    )
    holding(db_session)
    db_session.flush()
    monkeypatch.setattr(jade_replay, "today_et", lambda: days[-1])
    monkeypatch.setattr(jade_stress, "today_et", lambda: days[-1])


def request_body() -> dict[str, object]:
    return {
        "base_currency": "USD",
        "range": "1Y",
        "versions": [
            {"rule": "none", "rows": [{"kind": "etf", "symbol": "SPY", "amount": "100.00"}]}
        ],
    }


def test_b2_current(app_client: TestClient, snapshot: None) -> None:
    response = app_client.post("/jade/what-if", json=request_body())
    assert response.status_code == 200, response.text
    current = response.json()["columns"][0]
    for field, route in [("replay", "replay"), ("tail_risk", "tail-risk"), ("stress", "stress")]:
        response = app_client.get(
            "/jade/" + route, params={"base_currency": "USD", "benchmark": "sp500"}
        )
        assert response.status_code == 200
        assert current[field] == response.json()


def test_b3_etf(app_client: TestClient, snapshot: None) -> None:
    response = app_client.post("/jade/what-if", json=request_body())
    assert response.status_code == 200
    replay = response.json()["columns"][1]["replay"]
    assert replay["holdings"][0]["method"] == "own"
    assert float(replay["metrics"]["portfolio"]["cumulative_return"]) == pytest.approx(0.21)


def test_b4_stress(db_session: Session, snapshot: None) -> None:
    from app.schemas.jade import WhatIfRowIn
    from app.services.jade_replay_config import SCENARIOS
    from app.services.jade_what_if import what_if_holdings
    from app.services.portfolio_calculator import compute_portfolio
    from app.tests.test_jade_stress import DAYS, cached

    a = [Decimal(100 + i * 2) for i in range(len(DAYS))]
    b = [Decimal(100 - i) for i in range(len(DAYS))]
    cached(db_session, "yf:SPY", DAYS, [float(v) for v in a])
    cached(db_session, "yf:AGG", DAYS, [float(v) for v in b])
    hs = what_if_holdings(
        compute_portfolio(db_session, TEST_USER_ID, "USD").holdings,
        [WhatIfRowIn(kind="etf", symbol=s, amount="50") for s in ["SPY", "AGG"]],
        "USD",
    )
    out = jade_stress.compute_stress(
        db_session, TEST_USER_ID, "USD", "sp500", holdings=hs, rule="quarterly"
    ).scenarios[2]
    path = dict(
        jade_stress.rebalanced_path(
            DAYS,
            [
                (dict(zip(DAYS, a, strict=True)), Decimal(".5")),
                (dict(zip(DAYS, b, strict=True)), Decimal(".5")),
            ],
            "quarterly",
        )
    )
    assert out.status == "ok"
    assert out.contributions == []
    peak = SCENARIOS[2].peak
    assert [float(p.portfolio) for p in out.points] == pytest.approx(
        [v / path[peak] - 1 for v in path.values()], abs=1e-6
    )


def test_b5_validation(app_client: TestClient, db_session: Session, snapshot: None) -> None:
    from uuid import uuid4

    h = holding(db_session, value="0")
    other_id = uuid4()
    seed_user(db_session, other_id)
    other = holding(db_session)
    other.user_id = other_id
    db_session.flush()
    cases = [
        ([{"kind": "holding", "holding_id": str(other.id), "amount": "1"}], "holding_not_found"),
        ([{"kind": "etf", "symbol": "XYZ", "amount": "1"}], "symbol_not_allowed"),
        ([{"kind": "cash", "amount": "1"}] * 2, "duplicate_row"),
        ([{"kind": "cash", "amount": "-1"}], "invalid_amount"),
        ([{"kind": "cash", "amount": "1.001"}], "invalid_amount"),
        ([{"kind": "holding", "holding_id": str(h.id), "amount": "1"}], "holding_unvalued"),
        ([{"kind": "cash", "amount": "0"}], "empty_version"),
    ]
    for rows, code in cases:
        out = app_client.post(
            "/jade/what-if",
            json={"base_currency": "USD", "versions": [{"rule": "none", "rows": rows}]},
        )
        assert out.status_code == 422, out.text
        assert out.json()["detail"] == code
    for n in [0, 3]:
        assert (
            app_client.post(
                "/jade/what-if",
                json={
                    "base_currency": "USD",
                    "versions": [{"rule": "none", "rows": [{"kind": "cash", "amount": "1"}]}] * n,
                },
            ).status_code
            == 422
        )


def test_b6_cooldown(app_client: TestClient, snapshot: None) -> None:
    invalid = {
        "base_currency": "USD",
        "versions": [{"rule": "none", "rows": [{"kind": "cash", "amount": "-1"}]}],
    }
    assert app_client.post("/jade/what-if", json=invalid).status_code == 422
    assert app_client.post("/jade/what-if", json=request_body()).status_code == 200
    second = app_client.post("/jade/what-if", json=request_body())
    assert second.status_code == 429
    assert second.json()["detail"] == "what_if_cooldown"
    assert 0 < int(second.headers["Retry-After"]) <= 120


def test_b7_access(app_client: TestClient, db_session: Session, snapshot: None) -> None:
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    user.subscription_type = "daily"
    db_session.flush()
    out = app_client.post("/jade/what-if", json=request_body())
    assert out.status_code == 403
    assert out.json()["detail"] == "subscription_required"


def test_b8_picker() -> None:
    from app.schemas.jade import WhatIfRowIn
    from app.services.jade_replay_config import FIXED_ETF_SYMBOLS, WHAT_IF_SYMBOLS
    from app.services.jade_what_if import what_if_holdings

    assert set(WHAT_IF_SYMBOLS) <= FIXED_ETF_SYMBOLS
    hs = what_if_holdings(
        [], [WhatIfRowIn(kind="etf", symbol=s, amount="1") for s in WHAT_IF_SYMBOLS], "USD"
    )
    assert [jade_replay.proxy_for(h).symbol for h in hs] == list(WHAT_IF_SYMBOLS)
