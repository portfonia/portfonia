"""Credit read-path contract against real Postgres."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import ET, today_et
from app.models.credit_ledger import CreditLedgerEntry
from app.models.user import User
from app.services.credit_ledger import adjust_by_admin, grant_signup_credits
from app.tests.conftest import seed_user


def _rows(response_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(response_text)))


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}


def test_ledger_csv_contract(app_client: TestClient, db_session: Session) -> None:
    live = seed_user(db_session, uuid.uuid4(), "live@example.com")
    removed = seed_user(db_session, uuid.uuid4(), "removed@example.com")
    grant_signup_credits(db_session, live)
    grant_signup_credits(db_session, removed)
    deleted_at = datetime(2026, 9, 27, 2, 45, 19, tzinfo=ET)
    db_session.query(CreditLedgerEntry).filter_by(user_id=removed.id).update(
        {CreditLedgerEntry.user_deleted_at: deleted_at}
    )
    db_session.delete(removed)
    db_session.flush()

    assert app_client.get("/admin/credits/ledger.csv").status_code == 401
    response = app_client.get("/admin/credits/ledger.csv", headers=_headers())
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        f"attachment; filename=credit-ledger-{today_et()}.csv"
    )
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines()[0] == (
        "id,created_at,user_id,email,bucket,amount,balance_after,reason,actor_type,"
        "actor_id,idempotency_key,reference,note,user_deleted_at"
    )
    rows = _rows(response.text)
    assert [int(row["id"]) for row in rows] == sorted(int(row["id"]) for row in rows)
    selected = {row["user_id"]: row for row in rows}
    assert selected[str(live.id)]["email"] == "live@example.com"
    assert selected[str(removed.id)]["email"] == ""
    assert selected[str(removed.id)]["user_deleted_at"] == deleted_at.isoformat()
    for row in selected.values():
        assert row["amount"] == row["balance_after"] == "5.00"
        assert datetime.fromisoformat(row["created_at"]).utcoffset() == deleted_at.utcoffset()


def test_balances_csv_consistency(app_client: TestClient, db_session: Session) -> None:
    bob = seed_user(db_session, uuid.uuid4(), "bob@example.com")
    grant_signup_credits(db_session, bob)
    adjust_by_admin(
        db_session,
        user_id=bob.id,
        amount=Decimal("10.00"),
        note="thanks",
        idempotency_key=str(uuid.uuid4()),
    )
    adjust_by_admin(
        db_session,
        user_id=bob.id,
        amount=Decimal("-3.00"),
        note="correction",
        idempotency_key=str(uuid.uuid4()),
    )
    assert app_client.get("/admin/credits/balances.csv").status_code == 401
    response = app_client.get("/admin/credits/balances.csv", headers=_headers())
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        f"attachment; filename=credit-balances-{today_et()}.csv"
    )
    assert response.text.splitlines()[0] == (
        "user_id,email,status,cash_balance,gift_balance,total_balance,"
        "ledger_cash_sum,ledger_gift_sum,consistent"
    )
    rows = _rows(response.text)
    assert [row["email"] for row in rows] == sorted(row["email"] for row in rows)
    assert next(row for row in rows if row["user_id"] == str(bob.id)) == {
        "user_id": str(bob.id),
        "email": "bob@example.com",
        "status": "active",
        "cash_balance": "0.00",
        "gift_balance": "12.00",
        "total_balance": "12.00",
        "ledger_cash_sum": "0.00",
        "ledger_gift_sum": "12.00",
        "consistent": "true",
    }
    db_session.execute(
        update(User).where(User.id == bob.id).values(credit_gift_balance=Decimal("11.00"))
    )
    response = app_client.get("/admin/credits/balances.csv", headers=_headers())
    broken = next(row for row in _rows(response.text) if row["user_id"] == str(bob.id))
    assert broken["gift_balance"] == "11.00"
    assert broken["ledger_gift_sum"] == "12.00"
    assert broken["consistent"] == "false"
