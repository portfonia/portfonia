"""Issue #783 focused acceptance checks on PostgreSQL."""

from datetime import datetime
from decimal import Decimal
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.timezones import today_et
from app.models.holding import Holding
from app.models.holding_change_group import HoldingChangeGroup
from app.models.user import User
from app.services import holding_changes, holdings_write
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_holding_changes import writer  # noqa: F401


@pytest.fixture(autouse=True)
def capture(monkeypatch: pytest.MonkeyPatch) -> Mock:
    mock = Mock()
    monkeypatch.setattr(holdings_write, "_enqueue_sparse_for", mock)
    return mock


def seed(session: Session, count: int = 2) -> list[Holding]:
    if session.get(User, TEST_USER_ID) is None:
        seed_user(session, TEST_USER_ID)
    rows = [
        Holding(
            id=uuid4(),
            user_id=TEST_USER_ID,
            name=f"Row {i}",
            ticker="AAPL",
            currency="USD",
            pricing_mode="auto",
            market="US",
            asset_type="stock",
            shares=Decimal("10"),
            position=i,
        )
        for i in range(count)
    ]
    session.add_all(rows)
    session.commit()
    return rows


def book(session: Session) -> holding_changes.Snapshot:
    session.expire_all()
    return holding_changes.snapshot(
        session.scalars(select(Holding).where(Holding.user_id == TEST_USER_ID)).all()
    )


def groups(session: Session) -> list[HoldingChangeGroup]:
    return list(
        session.scalars(
            select(HoldingChangeGroup)
            .where(HoldingChangeGroup.user_id == TEST_USER_ID)
            .order_by(HoldingChangeGroup.created_at, HoldingChangeGroup.id)
        ).all()
    )


def preview(client: TestClient, group: HoldingChangeGroup) -> dict[str, object]:
    response = client.get(f"/holdings/changes/{group.id}/rollback")
    assert response.status_code == 200, response.text
    return cast(dict[str, object], response.json())


def execute(client: TestClient, group: HoldingChangeGroup, version: object) -> int:
    return client.post(
        f"/holdings/changes/{group.id}/rollback", json={"version": version}
    ).status_code


def test_01_restore_before_three_groups(app_client: TestClient, db_session: Session) -> None:
    a, b = seed(db_session)
    before = book(db_session)
    assert app_client.patch(f"/holdings/{a.id}", json={"shares": 12}).status_code == 200
    assert app_client.delete(f"/holdings/{b.id}").status_code == 204
    assert (
        app_client.post(
            "/holdings/confirm",
            json=[
                {"name": "Added", "currency": "USD", "pricing_mode": "manual", "current_value": 20}
            ],
        ).status_code
        == 200
    )
    target = groups(db_session)[0]
    result = preview(app_client, target)
    assert {k: result[k] for k in ("restored", "removed", "modified", "groups")} == {
        "restored": 1,
        "removed": 1,
        "modified": 1,
        "groups": 3,
    }
    assert execute(app_client, target, result["version"]) == 204
    assert book(db_session) == before


def test_02_rollback_is_reversible(app_client: TestClient, db_session: Session) -> None:
    a = seed(db_session, 1)[0]
    assert app_client.patch(f"/holdings/{a.id}", json={"shares": 12}).status_code == 200
    changed = book(db_session)
    target = groups(db_session)[0]
    assert execute(app_client, target, preview(app_client, target)["version"]) == 204
    rollback = groups(db_session)[-1]
    assert rollback.source == "rollback"
    assert rollback.rollback_to == target.created_at
    assert execute(app_client, rollback, preview(app_client, rollback)["version"]) == 204
    assert book(db_session) == changed


def test_03_ops_barrier_preserves_untouched_field(
    app_client: TestClient, db_session: Session
) -> None:
    a = seed(db_session, 1)[0]
    assert app_client.patch(f"/holdings/{a.id}", json={"notes": "first"}).status_code == 200
    assert app_client.patch(f"/holdings/{a.id}", json={"shares": 12}).status_code == 200
    a.shares = Decimal("99")
    db_session.commit()
    assert app_client.patch(f"/holdings/{a.id}", json={"notes": "newer"}).status_code == 200
    ordered = groups(db_session)
    result = app_client.get(
        "/holdings/changes", params={"start": str(today_et()), "end": str(today_et())}
    ).json()
    assert {item["id"]: item["rollback_available"] for item in result["items"]} == {
        str(ordered[0].id): False,
        str(ordered[1].id): False,
        str(ordered[2].id): True,
    }
    before = book(db_session)
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    response = app_client.post(
        f"/holdings/changes/{ordered[1].id}/rollback", json={"version": str(user.holdings_version)}
    )
    assert response.status_code == 409 and response.json()["detail"] == "rollback_blocked"
    assert book(db_session) == before and len(groups(db_session)) == 3
    assert execute(app_client, ordered[-1], preview(app_client, ordered[-1])["version"]) == 204
    assert book(db_session)[a.id]["shares"] == "99"
    assert book(db_session)[a.id]["notes"] == "first"


def test_04_stale_version_changes_nothing(app_client: TestClient, db_session: Session) -> None:
    a = seed(db_session, 1)[0]
    assert app_client.patch(f"/holdings/{a.id}", json={"shares": 12}).status_code == 200
    target = groups(db_session)[0]
    result = preview(app_client, target)
    assert app_client.patch(f"/holdings/{a.id}", json={"shares": 13}).status_code == 200
    before = book(db_session)
    ids = [g.id for g in groups(db_session)]
    response = app_client.post(
        f"/holdings/changes/{target.id}/rollback", json={"version": result["version"]}
    )
    assert response.status_code == 409 and response.json()["detail"] == "version_conflict"
    assert book(db_session) == before and [g.id for g in groups(db_session)] == ids


def test_05_over_cap_restore_is_refused(app_client: TestClient, db_session: Session) -> None:
    rows = seed(db_session, 40)
    before = holding_changes.snapshot(rows[:-2])
    removed = Holding(
        id=uuid4(),
        user_id=TEST_USER_ID,
        name="Deleted",
        currency="USD",
        pricing_mode="manual",
        market="Other",
        position=40,
    )
    before.update(holding_changes.snapshot([removed]))
    holdings_write.lock_user_book(db_session, TEST_USER_ID)
    holding_changes.record(db_session, TEST_USER_ID, before, source="web")
    db_session.commit()
    target = groups(db_session)[0]
    result = preview(app_client, target)
    assert result["restored"] == 1 and result["removed"] == 2
    response = app_client.post(
        f"/holdings/changes/{target.id}/rollback", json={"version": result["version"]}
    )
    assert response.status_code == 409 and response.json()["detail"] == "holding_cap"
    assert len(book(db_session)) == 40 and len(groups(db_session)) == 1


def test_06_restore_system_fields_and_exact_market(
    app_client: TestClient, db_session: Session, capture: Mock
) -> None:
    a, untouched = seed(db_session)
    a.market = "Other"
    a.broker = "Custodian"
    a.asset_class = "BOND_FUND"
    a.capture_supported = False
    untouched.asset_class = "BOND_FUND"
    untouched.capture_supported = False
    db_session.commit()
    untouched_updated = untouched.updated_at
    assert app_client.delete(f"/holdings/{a.id}").status_code == 204
    target = groups(db_session)[0]
    result = preview(app_client, target)
    capture.reset_mock()
    assert execute(app_client, target, result["version"]) == 204
    book(db_session)
    restored = db_session.get(Holding, a.id)
    assert restored is not None and restored.market == "Other"
    expected = holdings_write.apply_write_defaults(dict(holding_changes.snapshot([restored])[a.id]))
    assert (
        restored.asset_class == expected["asset_class"]
        and restored.capture_supported == expected["capture_supported"]
    )
    assert restored.account_id is not None
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None and str(user.holdings_version) == str(int(str(result["version"])) + 1)
    capture.assert_called_once()
    assert capture.call_args.args[2] == [restored]
    assert capture.call_args.kwargs == {"log_prefix": "rollback_holdings"}
    db_session.refresh(untouched)
    assert untouched.asset_class == "BOND_FUND" and untouched.capture_supported is False
    assert untouched.updated_at == untouched_updated


def test_07_agent_history_has_only_shared_fields(
    app_client: TestClient,
    db_session: Session,
    writer: dict[str, str],  # noqa: F811
) -> None:
    a = seed(db_session, 1)[0]
    assert app_client.patch(f"/holdings/{a.id}", json={"shares": 12}).status_code == 200
    target = groups(db_session)[0]
    assert execute(app_client, target, preview(app_client, target)["version"]) == 204
    response = app_client.get(
        "/agent/v1/holdings/changes",
        headers=writer,
        params={"start": str(today_et()), "end": str(today_et())},
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert (
        item["source"] == "rollback"
        and datetime.fromisoformat(item["rollback_to"]) == target.created_at
    )
    assert all("rollback_available" not in item for item in response.json()["items"])


def test_10_group_timestamp_is_insert_time(app_client: TestClient, db_session: Session) -> None:
    a = seed(db_session, 1)[0]
    transaction_time = db_session.scalar(text("SELECT now()"))
    db_session.execute(text("SELECT pg_sleep(0.02)"))
    assert app_client.patch(f"/holdings/{a.id}", json={"shares": 12}).status_code == 200
    assert groups(db_session)[0].created_at > transaction_time
