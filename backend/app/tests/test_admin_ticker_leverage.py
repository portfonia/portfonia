"""GET/POST/PATCH/DELETE /admin/ticker-leverage (issue #87).

Ops CRUD for the system-wide ticker -> leverage_multiple table, same auth/
audit-logging shape as every other /admin/* route: router-level
ADMIN_API_TOKEN dependency, no user-auth involvement.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.ticker_leverage import TickerLeverageOverride
from app.tests.test_admin_router import _headers


def test_create_ticker_leverage_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/ticker-leverage", json={"ticker": "MUU", "leverage_multiple": "2.0"}
    )
    assert resp.status_code == 401


def test_create_ticker_leverage_normalizes_ticker(
    app_client: TestClient, db_session: Session
) -> None:
    """A lowercase input ticker must be stored normalized+uppercased — the
    same PK form window_data.py/portfolio_calculator.py look up by."""
    resp = app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "muu", "leverage_multiple": "2.0", "direction": "bull"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["ticker"] == "MUU"
    assert body["leverage_multiple"] == "2.0"
    assert body["direction"] == "bull"
    row = db_session.get(TickerLeverageOverride, "MUU")
    assert row is not None
    assert row.leverage_multiple == Decimal("2.0")


def test_create_ticker_leverage_conflicts_on_existing_ticker(
    app_client: TestClient,
) -> None:
    first = app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "MUU", "leverage_multiple": "2.0"},
    )
    assert first.status_code == 201
    dup = app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "muu", "leverage_multiple": "3.0"},
    )
    assert dup.status_code == 409


def test_create_ticker_leverage_rejects_non_positive_multiple(app_client: TestClient) -> None:
    resp = app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "MUU", "leverage_multiple": "0"},
    )
    assert resp.status_code == 422


def test_list_ticker_leverage(app_client: TestClient) -> None:
    app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "MUU", "leverage_multiple": "2"},
    )
    app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "SOXL", "leverage_multiple": "3"},
    )
    resp = app_client.get("/admin/ticker-leverage", headers=_headers())
    assert resp.status_code == 200
    tickers = {row["ticker"] for row in resp.json()}
    assert tickers == {"MUU", "SOXL"}


def test_get_ticker_leverage_404_unknown(app_client: TestClient) -> None:
    resp = app_client.get("/admin/ticker-leverage/NOPE", headers=_headers())
    assert resp.status_code == 404


def test_get_ticker_leverage_lowercase_lookup_matches_stored_uppercase(
    app_client: TestClient,
) -> None:
    app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "MUU", "leverage_multiple": "2"},
    )
    resp = app_client.get("/admin/ticker-leverage/muu", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "MUU"


def test_update_ticker_leverage_partial_leaves_other_fields_unchanged(
    app_client: TestClient, db_session: Session
) -> None:
    app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "MUU", "leverage_multiple": "2", "notes": "Direxion 2x MU"},
    )
    resp = app_client.patch(
        "/admin/ticker-leverage/MUU", headers=_headers(), json={"leverage_multiple": "3"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["leverage_multiple"] == "3"
    assert body["notes"] == "Direxion 2x MU"  # untouched by the partial patch


def test_update_ticker_leverage_can_explicitly_clear_notes(app_client: TestClient) -> None:
    app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "MUU", "leverage_multiple": "2", "notes": "Direxion 2x MU"},
    )
    resp = app_client.patch("/admin/ticker-leverage/MUU", headers=_headers(), json={"notes": None})
    assert resp.status_code == 200
    assert resp.json()["notes"] is None


def test_update_ticker_leverage_404_unknown(app_client: TestClient) -> None:
    resp = app_client.patch(
        "/admin/ticker-leverage/NOPE", headers=_headers(), json={"leverage_multiple": "2"}
    )
    assert resp.status_code == 404


def test_delete_ticker_leverage(app_client: TestClient, db_session: Session) -> None:
    app_client.post(
        "/admin/ticker-leverage",
        headers=_headers(),
        json={"ticker": "MUU", "leverage_multiple": "2"},
    )
    resp = app_client.delete("/admin/ticker-leverage/MUU", headers=_headers())
    assert resp.status_code == 204
    db_session.expire_all()
    assert db_session.get(TickerLeverageOverride, "MUU") is None


def test_delete_ticker_leverage_404_unknown(app_client: TestClient) -> None:
    resp = app_client.delete("/admin/ticker-leverage/NOPE", headers=_headers())
    assert resp.status_code == 404
