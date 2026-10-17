"""Issue #785 acceptance checks; delivery and enqueue are mocked."""

# ruff: noqa: F811

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.rate_limit import get_backend
from app.core.timezones import ET, today_et
from app.models.holding import Holding
from app.models.holding_change_group import HoldingChangeGroup
from app.models.price_snapshot import PriceSnapshot
from app.models.user import User
from app.services import holding_changes, holdings_write
from app.services.portfolio_calculator import _latest_captured_closes
from app.tests.conftest import TEST_USER_ID
from app.tests.test_holding_changes import writer as writer_headers  # noqa: F401
from app.tests.test_holding_rollback import groups
from app.tests.test_holding_rollback import seed as _seed


def seed(session: Session, count: int = 2) -> list[Holding]:
    rows = _seed(session, count)
    user = session.get(User, TEST_USER_ID)
    assert user is not None
    user.email_verified_at = datetime.now(ET)
    session.commit()
    return rows


@pytest.fixture(autouse=True)
def delivery(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Mock:
    monkeypatch.setattr(logging.getLogger("app.services.holding_change_alerts"), "disabled", False)
    send = Mock(return_value=True)
    if request.node.name != "test_08_email_content":
        monkeypatch.setattr(
            "app.services.email_sender.send_holdings_change_alert", send, raising=False
        )
    monkeypatch.setattr(holdings_write, "_enqueue_sparse_for", Mock())
    monkeypatch.setattr(holdings_write, "_enqueue_replaced_book", Mock())
    return send


def evaluate(session: Session, group: HoldingChangeGroup) -> None:
    from app.services.holding_change_alerts import evaluate_change_alert

    evaluate_change_alert(session, TEST_USER_ID, group.id)


def patch(
    client: TestClient, session: Session, headers: dict[str, str], row: Holding, shares: int
) -> HoldingChangeGroup:
    user = session.get(User, TEST_USER_ID)
    assert user is not None
    version = str(user.holdings_version)
    response = client.patch(
        f"/agent/v1/holdings/{row.id}",
        headers=headers,
        json={"version": version, "changes": {"shares": str(shares)}},
    )
    assert response.status_code == 200, response.text
    return groups(session)[-1]


def priced(session: Session) -> Holding:
    row = seed(session, 1)[0]
    row.shares = Decimal(100)
    session.add(
        PriceSnapshot(
            ticker="AAPL",
            market="US",
            session_node="close",
            trade_date=today_et(),
            close=Decimal(1),
        )
    )
    session.commit()
    return row


def test_01_agent_net_deletion(
    app_client: TestClient, db_session: Session, writer_headers: dict[str, str], delivery: Mock
) -> None:
    rows = seed(db_session, 10)
    # Unpriced rows isolate the net deletion threshold.
    for keep, expected in [(8, 0), (7, 1)]:
        user = db_session.get(User, TEST_USER_ID)
        assert user is not None
        version = str(user.holdings_version)
        response = app_client.put(
            "/agent/v1/holdings",
            headers=writer_headers,
            json={
                "version": version,
                "holdings": [
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "ticker": row.ticker,
                        "currency": row.currency,
                        "pricing_mode": row.pricing_mode,
                        "shares": str(row.shares),
                        "market": row.market,
                        "asset_type": row.asset_type,
                    }
                    for row in rows[:keep]
                ],
                "delete_ids": [str(row.id) for row in rows[keep:]],
            },
        )
        assert response.status_code == 200, response.text
        evaluate(db_session, groups(db_session)[-1])
        assert delivery.call_count == expected
        if keep == 8:
            # Restore ten rows for the exact 30% boundary.
            rows = rows[:8] + seed(db_session, 2)


def test_02_file_replace_unchanged(db_session: Session, delivery: Mock) -> None:
    from app.schemas.holdings import ParsedRow

    row = priced(db_session)
    holdings_write.confirm_holdings(
        db_session,
        TEST_USER_ID,
        [
            ParsedRow(
                name=row.name,
                ticker=row.ticker,
                currency=row.currency,
                pricing_mode="auto",
                shares=100,
                market="US",
                asset_type="stock",
            )
        ],
        "replace",
    )
    evaluate(db_session, groups(db_session)[-1])
    delivery.assert_not_called()


def test_03_value_drop(
    app_client: TestClient, db_session: Session, writer_headers: dict[str, str], delivery: Mock
) -> None:
    row = priced(db_session)
    evaluate(db_session, patch(app_client, db_session, writer_headers, row, 71))
    delivery.assert_not_called()
    assert app_client.patch(f"/holdings/{row.id}", json={"shares": 100}).status_code == 200
    evaluate(db_session, patch(app_client, db_session, writer_headers, row, 70))
    assert delivery.call_args.kwargs["signals"] == ["drop"]
    assert delivery.call_args.kwargs["value_change_pct"] == -30


def test_04_value_rise(
    app_client: TestClient, db_session: Session, writer_headers: dict[str, str], delivery: Mock
) -> None:
    row = priced(db_session)
    evaluate(db_session, patch(app_client, db_session, writer_headers, row, 199))
    delivery.assert_not_called()
    assert app_client.patch(f"/holdings/{row.id}", json={"shares": 100}).status_code == 200
    evaluate(db_session, patch(app_client, db_session, writer_headers, row, 200))
    assert delivery.call_args.kwargs["signals"] == ["rise"]
    assert delivery.call_args.kwargs["value_change_pct"] == 100


def test_05_unpriced_before(db_session: Session, delivery: Mock) -> None:
    rows = seed(db_session, 2)
    before = holding_changes.snapshot(rows)
    rows[0].shares = Decimal(200)
    holding_changes.record(db_session, TEST_USER_ID, before, source="file")
    db_session.commit()
    evaluate(db_session, groups(db_session)[-1])
    delivery.assert_not_called()
    before = holding_changes.snapshot(rows)
    db_session.delete(rows[1])
    holding_changes.record(db_session, TEST_USER_ID, before, source="file")
    db_session.commit()
    evaluate(db_session, groups(db_session)[-1])
    assert delivery.call_args.kwargs["signals"] == ["deletion"]
    assert delivery.call_args.kwargs["value_change_pct"] is None


def test_06_write_day_marker_and_independent_notice(
    app_client: TestClient,
    db_session: Session,
    writer_headers: dict[str, str],
    delivery: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = Mock()
    monkeypatch.setattr(
        "app.tasks.notification_tasks.send_api_modification_notice_task.delay", notice
    )
    row = priced(db_session)
    first = patch(app_client, db_session, writer_headers, row, 200)
    yesterday = today_et() - timedelta(days=1)
    first.created_at = datetime.combine(yesterday, datetime.min.time(), ET)
    db_session.commit()
    evaluate(db_session, first)
    assert get_backend().ttl(f"holdings:alert:{TEST_USER_ID}:{yesterday}") > 0
    second = patch(app_client, db_session, writer_headers, row, 400)
    second.created_at = first.created_at + timedelta(seconds=1)
    db_session.commit()
    evaluate(db_session, second)
    assert delivery.call_count == 1
    assert notice.call_count == 1


def test_07_web_and_rollback_excluded(
    app_client: TestClient, db_session: Session, delivery: Mock
) -> None:
    row = priced(db_session)
    assert app_client.patch(f"/holdings/{row.id}", json={"shares": 200}).status_code == 200
    group = groups(db_session)[-1]
    evaluate(db_session, group)
    user = db_session.get(User, TEST_USER_ID)
    assert user is not None
    version = str(user.holdings_version)
    holdings_write.rollback_holdings(db_session, TEST_USER_ID, group.id, version)
    evaluate(db_session, groups(db_session)[-1])
    delivery.assert_not_called()


def test_08_email_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import email_sender

    monkeypatch.setattr(email_sender, "get_settings", Mock(return_value=Mock()))
    transport = MagicMock()
    monkeypatch.setattr(httpx, "Client", transport)
    for source in ("agent", "file"):
        assert email_sender.send_holdings_change_alert(
            "test@example.com",
            locale="en",
            signals=["deletion", "drop"],
            changed_at_et="2026-10-16 12:00 ET",
            source=source,
            token=("writer", "prefix123456"),
            rows_before=10,
            rows_after=7,
            value_change_pct=-30,
            revoke_url="https://example.com/revoke",
        )
        payload = transport.return_value.__enter__.return_value.post.call_args.kwargs["json"]
        copy = email_sender._HOLDINGS_CHANGE_ALERT_COPY["en"]
        assert payload["subject"] == copy["subject"]
        assert copy["deletion"] in payload["text"] and copy["drop"] in payload["text"]
        assert copy["rows"].format(rows_before=10, rows_after=7) in payload["text"]
        assert copy["value"].format(value_change_pct=-30) in payload["text"]
        assert ("https://example.com/revoke" in payload["text"]) == (source == "agent")


def test_09_enqueue_failure_after_commit(
    app_client: TestClient,
    db_session: Session,
    writer_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.tasks.notification_tasks import send_holdings_change_alert_task

    row = seed(db_session, 1)[0]
    monkeypatch.setattr(
        send_holdings_change_alert_task, "delay", Mock(side_effect=RuntimeError("offline"))
    )
    group = patch(app_client, db_session, writer_headers, row, 30)
    assert group.source == "agent"
    db_session.expire_all()
    saved = db_session.get(Holding, row.id)
    assert saved is not None and saved.shares == Decimal(30)
    assert any(
        record.levelname == "ERROR" and str(TEST_USER_ID) in record.message
        for record in caplog.records
    )


def test_11_close_filter(db_session: Session) -> None:
    db_session.add_all(
        [
            PriceSnapshot(
                ticker=ticker,
                market="US",
                session_node="close",
                trade_date=today_et(),
                close=Decimal(1),
            )
            for ticker in ("AAPL", "MSFT")
        ]
    )
    db_session.flush()
    assert set(_latest_captured_closes(db_session)) == {"AAPL", "MSFT"}
    assert set(_latest_captured_closes(db_session, only={"AAPL"})) == {"AAPL"}
