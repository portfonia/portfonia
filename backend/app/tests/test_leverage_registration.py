"""Focused #839 registration and detection tests."""

from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.ticker_leverage import TickerLeverageOverride
from app.scripts.reclassify_asset_class import reclassify_asset_class
from app.services import holding_parser, holdings_write, report_sections, ticker_leverage
from app.tasks import admin_tasks, capture_tasks
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_admin_router import _headers


@pytest.fixture(autouse=True)
def _mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(holding_parser, "_load_ticker_asset_class", lambda: {"MAPPED": "STOCK"})
    monkeypatch.setattr(holdings_write, "_load_ticker_asset_class", lambda: {"MAPPED": "STOCK"})


def _holding(ticker: str = "MUU", **kwargs: object) -> Holding:
    data: dict[str, object] = dict(
        user_id=TEST_USER_ID,
        name="MU-Bull-2X",
        ticker=ticker,
        currency="USD",
        shares=Decimal("1"),
        asset_type="etf",
        asset_class="EQUITY_BROAD",
        market="US",
        pricing_mode="auto",
        capture_supported=True,
    )
    data.update(kwargs)
    return Holding(**data)


def _registration(session: Session, **kwargs: object) -> None:
    session.add(
        TickerLeverageOverride(
            ticker="MUU", leverage_multiple=Decimal("2.0"), created_by=TEST_USER_ID, **kwargs
        )
    )
    session.flush()


def test_write_defaults_registration_precedence() -> None:
    data = dict(ticker="MUU", asset_type="etf", currency="USD", market="US")
    assert (
        holdings_write.apply_write_defaults(data.copy(), {"MUU": "STOCK"})["asset_class"] == "STOCK"
    )
    assert holdings_write.apply_write_defaults(data.copy(), {})["asset_class"] == "EQUITY_BROAD"


def test_admin_registration_reclassifies_encrypted_holdings(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    holding = _holding()
    db_session.add(holding)
    _registration(db_session)
    response = app_client.patch(
        "/admin/ticker-leverage/MUU",
        headers=_headers(),
        json={"asset_class": "STOCK", "underlying_ticker": "mu"},
    )
    assert response.status_code == 200
    assert response.json()["reclassified_holdings"] == 1
    assert response.json()["underlying_ticker"] == "MU"
    db_session.refresh(holding)
    assert holding.ticker == "MUU" and holding.asset_class == "STOCK"
    assert app_client.delete("/admin/ticker-leverage/MUU", headers=_headers()).status_code == 204
    db_session.refresh(holding)
    assert holding.asset_class == "EQUITY_BROAD"


def test_admin_rejects_unknown_asset_class(app_client: TestClient) -> None:
    assert (
        app_client.post(
            "/admin/ticker-leverage",
            headers=_headers(),
            json={"ticker": "MUU", "leverage_multiple": "2", "asset_class": "NOT_A_CLASS"},
        ).status_code
        == 422
    )
    assert (
        app_client.patch(
            "/admin/ticker-leverage/MUU", headers=_headers(), json={"asset_class": "NOT_A_CLASS"}
        ).status_code
        == 422
    )


def test_sweep_preserves_registered_class(db_session: Session) -> None:
    seed_user(db_session, TEST_USER_ID)
    holding = _holding(asset_class="STOCK")
    db_session.add(holding)
    _registration(db_session, asset_class="STOCK")
    assert reclassify_asset_class(db_session, apply_changes=True) == 0
    assert holding.asset_class == "STOCK"


def test_registered_class_suppresses_unmapped_alert(db_session: Session) -> None:
    _registration(db_session, asset_class="STOCK")
    with patch("app.tasks.admin_tasks.send_admin_alert_task.delay") as send:
        holdings_write.enqueue_unmapped_asset_class_alert([_holding()], "web", db_session)
    send.assert_not_called()


def test_capture_includes_underlying_earnings(db_session: Session) -> None:
    _registration(db_session, underlying_ticker="MU")
    with (
        patch("app.core.database.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch("app.services.price_capture._market_tickers", return_value=["MUU"]),
        patch("app.services.forward_events.fetch_fred_release_dates", return_value=[]),
        patch("app.services.forward_events.fetch_fomc_dates", return_value=[]),
        patch("app.services.forward_events.fetch_earnings_dates", return_value=[]) as fetch,
        patch("app.services.forward_events.persist_forward_events", return_value=0),
    ):
        capture_tasks.capture_forward_events_task()
    assert fetch.call_args.args[0] == ["MU", "MUU"]


def test_forward_leverage_labels_and_old_inputs() -> None:
    event = {"event_type": "earnings", "ticker": "MU", "name": "MU earnings"}
    holdings = [{"ticker": "MUU", "name": "MU-Bull-2X"}]
    links = {"MUU": {"underlying": "MU", "multiple": "2", "direction": "bull"}}
    assert report_sections._forward_exposure(event, holdings, links)[0] == ["MU-Bull-2X (2x MU)"]
    links["MUU"]["direction"] = "bear"
    assert report_sections._forward_exposure(event, holdings, links)[0] == ["MU-Bull-2X (-2x MU)"]
    assert report_sections._forward_exposure(event, holdings)[0] == ["MU"]


def test_enqueue_excludes_registered_mapped_marked_and_unsupported(db_session: Session) -> None:
    _registration(db_session)
    holdings = [_holding(t) for t in ("MUU", "MAPPED", "MARKED", "NEW")]
    holdings.append(_holding("UNSUPPORTED", capture_supported=False))
    with (
        patch(
            "app.core.alert_dedup.already_alerted",
            side_effect=lambda key: key == "leverage_check:MARKED",
        ),
        patch("app.tasks.admin_tasks.check_leveraged_tickers_task.delay") as enqueue,
    ):
        holdings_write.enqueue_leverage_check(db_session, holdings)
    enqueue.assert_called_once_with(["NEW"])


def test_detection_marks_successes_and_task_alerts_once() -> None:
    ticker = MagicMock()
    ticker.get_info.side_effect = [
        {"category": "Trading--Leveraged Equity", "longName": "Product", "fundFamily": "Family"},
        {"category": None},
        RuntimeError("provider down"),
    ]
    with (
        patch("yfinance.Ticker", return_value=ticker),
        patch("app.core.alert_dedup.mark_alerted") as mark,
    ):
        detected = ticker_leverage.detect_leveraged(["MUU", "PLAIN", "FAILED"])
    assert [product.ticker for product in detected] == ["MUU"]
    assert mark.call_args_list == [
        call("leverage_check:MUU", 30 * 24 * 3600),
        call("leverage_check:PLAIN", 30 * 24 * 3600),
    ]
    with (
        patch.object(ticker_leverage, "detect_leveraged", side_effect=[detected, []]),
        patch("app.tasks.admin_tasks.send_admin_alert_task.delay") as send,
    ):
        admin_tasks.check_leveraged_tickers_task(["MUU"])
        send.assert_called_once()
        admin_tasks.check_leveraged_tickers_task(["PLAIN"])
        send.assert_called_once()


def test_regenerate_render_reads_persisted_leverage_links(db_session: Session) -> None:
    from datetime import timedelta

    from app.models.report import Report
    from app.services import report_generator as rg
    from app.services.report_context import ReportContext
    from app.services.report_serializers import _serialize_portfolio
    from app.tests.test_report_generator import _FAKE_LLM_PASS2, _NOW, _TODAY, _portfolio_snap

    seed_user(db_session, TEST_USER_ID)
    portfolio = _serialize_portfolio(_portfolio_snap())
    portfolio["holdings"][0].update(ticker="MUU", name="MU-Bull-2X")
    ctx = ReportContext(
        portfolio_summary=portfolio,
        pass2_raw=_FAKE_LLM_PASS2,
        forward_events=[
            {
                "event_type": "earnings",
                "ticker": "MU",
                "name": "MU earnings",
                "scheduled_date": _TODAY.isoformat(),
            }
        ],
        leverage_links={"MUU": {"underlying": "MU", "multiple": "2", "direction": "bull"}},
    )
    label = "MU-Bull-2X (2x MU)"
    for index, with_links in enumerate((True, False)):
        inputs = ctx.to_jsonb()
        if not with_links:
            inputs.pop("leverage_links")
        report = Report(
            user_id=TEST_USER_ID,
            report_date=_TODAY + timedelta(days=index),
            report_type="incremental",
            session_node="manual",
            status="success",
            period_start=_NOW - timedelta(days=1),
            period_end=_NOW + timedelta(days=1),
            report_inputs=inputs,
        )
        db_session.add(report)
        db_session.commit()
        report_id = report.id
        db_session.expire(report)
        rendered = rg.regenerate_report(
            db_session, report_id, user_id=TEST_USER_ID, mode="render", output_lang="en"
        )
        assert rendered.report_md is not None
        calendar = rendered.report_md.split("## §2.5 Forward Calendar", 1)[1].split("## §3", 1)[0]
        if with_links:
            assert label in calendar
        else:
            assert label not in calendar
            assert "| MU earnings | MU |" in calendar
