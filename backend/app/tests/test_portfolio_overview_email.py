"""send_portfolio_overview_email / _build_portfolio_overview_markdown
(issue #202) — explicit "Send holdings overview" email, not a formal report.

Real Postgres for the recipient-resolution and compute_portfolio paths
(mirrors test_email_sender.py's real-user-row tests and
test_portfolio_calculator.py's Holding fixtures); Resend HTTP is mocked.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.fx_rate import FxRate
from app.models.holding import Holding
from app.models.user import User
from app.services.email_sender import (
    _build_portfolio_overview_markdown,
    _glossary_term,
    send_portfolio_overview_email,
)
from app.services.portfolio_calculator import compute_portfolio
from app.tasks import next_occurrence_for_cadence

_USER_ID = uuid.uuid4()
_FX_DATE = date(2026, 1, 2)


def _user(**overrides: object) -> User:
    return User(
        id=_USER_ID,
        auth_provider="supabase",
        auth_subject=f"sub-{_USER_ID}",
        email="acct@example.com",
        status="active",
        locale="en",
        base_currency="USD",
        report_cadence="mwf",
        **overrides,
    )


def _priced_stock(name: str, ticker: str, price: str, shares: str, position: int) -> Holding:
    return Holding(
        user_id=_USER_ID,
        name=name,
        pricing_mode="auto",
        ticker=ticker,
        currency="USD",
        shares=Decimal(shares),
        market_price=Decimal(price),
        asset_type="stock",
        asset_class="STOCK",
        broker="Fidelity",
        position=position,
    )


def _priced_stock_hkd(name: str, ticker: str, price: str, shares: str, position: int) -> Holding:
    return Holding(
        user_id=_USER_ID,
        name=name,
        pricing_mode="auto",
        ticker=ticker,
        currency="HKD",
        shares=Decimal(shares),
        market_price=Decimal(price),
        asset_type="stock",
        asset_class="STOCK",
        broker="Fidelity",
        position=position,
    )


def _priced_fund_no_ticker(name: str, fund_code: str, value: str, position: int) -> Holding:
    return Holding(
        user_id=_USER_ID,
        name=name,
        pricing_mode="manual",
        fund_code=fund_code,
        currency="USD",
        current_value=Decimal(value),
        asset_type="fund",
        asset_class="BOND_FUND",
        broker="Fidelity",
        position=position,
    )


def _unpriced_stock(name: str, ticker: str, position: int) -> Holding:
    return Holding(
        user_id=_USER_ID,
        name=name,
        pricing_mode="auto",
        ticker=ticker,
        currency="USD",
        shares=None,
        market_price=None,
        asset_type="stock",
        asset_class="STOCK",
        broker="Fidelity",
        position=position,
    )


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.RESEND_API_KEY.get_secret_value.return_value = "re_test_key"
    s.EMAIL_FROM = "Portfonia <portfonia@physicalclue.us>"
    s.EMAIL_REPLY_TO = "portfonia@physicalclue.us"
    return s


# ---------------------------------------------------------------------------
# _glossary_term
# ---------------------------------------------------------------------------


def test_glossary_term_en_returns_the_key_itself() -> None:
    assert _glossary_term("STOCK", "en") == "STOCK"


def test_glossary_term_zh_translates_known_asset_class() -> None:
    assert _glossary_term("STOCK", "zh") == "个股"


def test_glossary_term_unknown_key_falls_back_to_itself() -> None:
    assert _glossary_term("NOT_A_REAL_KEY", "zh") == "NOT_A_REAL_KEY"


# ---------------------------------------------------------------------------
# _build_portfolio_overview_markdown: every holding stays in the table
# (issue #202 decision 3), pending ones get a placeholder, never dropped.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _seed_fx_and_user(db_session: Session) -> None:
    db_session.add(FxRate(pair="USDCNY", rate=Decimal("7.0"), rate_date=_FX_DATE, source="test"))
    db_session.add(FxRate(pair="USDHKD", rate=Decimal("8.0"), rate_date=_FX_DATE, source="test"))
    db_session.add(_user())
    db_session.commit()


def test_markdown_lists_every_holding_priced_and_pending(db_session: Session) -> None:
    db_session.add_all(
        [
            _priced_stock("Apple", "AAPL", "150", "10", position=0),
            _unpriced_stock("New IPO Co", "NEWCO", position=1),
        ]
    )
    db_session.commit()
    snap = compute_portfolio(db_session, user_id=_USER_ID, base_currency="USD")

    md = _build_portfolio_overview_markdown(
        snap, "en", next_occurrence_for_cadence("mwf", datetime.now(tz=ET))
    )

    assert "Apple (AAPL)" in md
    assert "New IPO Co (NEWCO)" in md
    assert "price pending" in md
    assert "1 of 2 holdings priced; 1 pending" in md


def test_markdown_value_cell_is_in_base_currency_not_native_currency(
    db_session: Session,
) -> None:
    """Review 5100733033 blocker: an HKD holding's value cell must render
    market_value_base in the snapshot's base_currency (summable to the
    total and the % column) — not h.market_value in the holding's own
    currency, which silently ignored the page's selected base_currency."""
    db_session.add(_priced_stock_hkd("Tencent", "0700", "400", "10", position=0))
    db_session.commit()
    snap = compute_portfolio(db_session, user_id=_USER_ID, base_currency="USD")
    assert snap.base_currency == "USD"

    md = _build_portfolio_overview_markdown(
        snap, "en", next_occurrence_for_cadence("mwf", datetime.now(tz=ET))
    )

    # 400 HKD * 10 shares / 8.0 USDHKD = 500 USD.
    assert "USD 500.00" in md
    assert "HKD 4,000.00" not in md


def test_markdown_name_cell_falls_back_to_fund_code_when_no_ticker(
    db_session: Session,
) -> None:
    db_session.add(_priced_fund_no_ticker("Bond Fund", "F00001", "1000", position=0))
    db_session.commit()
    snap = compute_portfolio(db_session, user_id=_USER_ID, base_currency="USD")

    md = _build_portfolio_overview_markdown(
        snap, "en", next_occurrence_for_cadence("mwf", datetime.now(tz=ET))
    )

    assert "Bond Fund (F00001)" in md


def test_markdown_zh_locale_translates_asset_class_and_labels(db_session: Session) -> None:
    db_session.add(_priced_stock("Apple", "AAPL", "150", "10", position=0))
    db_session.commit()
    snap = compute_portfolio(db_session, user_id=_USER_ID, base_currency="USD")

    md = _build_portfolio_overview_markdown(
        snap, "zh", next_occurrence_for_cadence("mwf", datetime.now(tz=ET))
    )

    assert "个股" in md  # STOCK, via report_glossary
    assert "持仓总值" in md


# ---------------------------------------------------------------------------
# send_portfolio_overview_email
# ---------------------------------------------------------------------------


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_success(
    mock_client_cls: MagicMock,
    mock_settings: MagicMock,
    _recipient: MagicMock,
    db_session: Session,
) -> None:
    mock_settings.return_value = _mock_settings()
    db_session.add(_priced_stock("Apple", "AAPL", "150", "10", position=0))
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": "resend-id"}
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    mock_client_cls.return_value.__enter__.return_value = mock_client

    result = send_portfolio_overview_email(db_session, _USER_ID, "USD")

    assert result is True
    mock_client.post.assert_called_once()
    payload = mock_client.post.call_args.kwargs["json"]
    assert payload["to"] == ["test@example.com"]
    assert "Idempotency-Key" not in mock_client.post.call_args.kwargs["headers"]
    # Issue #350 item 3: the reused _build_footer disclaimer now follows
    # this user's own locale ("en", the autouse _seed_fx_and_user default)
    # rather than always rendering bilingual.
    assert "Disclaimer" in payload["html"]
    assert "免责声明" not in payload["html"]


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_footer_matches_zh_locale_user(
    mock_client_cls: MagicMock,
    mock_settings: MagicMock,
    _recipient: MagicMock,
    db_session: Session,
) -> None:
    mock_settings.return_value = _mock_settings()
    db_session.add(_priced_stock("Apple", "AAPL", "150", "10", position=0))
    user = db_session.get(User, _USER_ID)
    assert user is not None
    user.locale = "zh"
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": "resend-id"}
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    mock_client_cls.return_value.__enter__.return_value = mock_client

    result = send_portfolio_overview_email(db_session, _USER_ID, "USD")

    assert result is True
    payload = mock_client.post.call_args.kwargs["json"]
    assert "免责声明" in payload["html"]
    assert "Disclaimer" not in payload["html"]


@patch("app.services.email_sender.send_ops_alert")
@patch("app.services.email_sender.recipient_email_with_purpose", return_value=None)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_inactive_user_alerts_and_fails_closed(
    mock_client_cls: MagicMock,
    mock_settings: MagicMock,
    _recipient: MagicMock,
    mock_alert: MagicMock,
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_settings.return_value = _mock_settings()

    logging.getLogger("app.services.email_sender").disabled = False
    with caplog.at_level(logging.WARNING, logger="app.services.email_sender"):
        result = send_portfolio_overview_email(db_session, uuid.uuid4(), "USD")

    assert result is False
    mock_client_cls.assert_not_called()
    mock_alert.assert_called_once()
    assert (
        mock_alert.call_args.kwargs["subject"]
        == "Portfonia: portfolio overview recipient could not be resolved"
    )


@patch("app.services.email_sender.send_ops_alert")
@patch("app.services.email_sender.recipient_email_with_purpose", return_value=None)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_active_unverified_user_still_alerts(
    mock_client_cls: MagicMock,
    mock_settings: MagicMock,
    _recipient: MagicMock,
    mock_alert: MagicMock,
    db_session: Session,
) -> None:
    """issue #202 comment 1 decision 2: unlike a downgrade-to-log-only
    proposal that was rejected, this branch alerts every time, at parity
    with send_report_email — even though the caller fires unconditionally
    on every button click that clears the cooldown."""
    mock_settings.return_value = _mock_settings()

    result = send_portfolio_overview_email(db_session, _USER_ID, "USD")

    assert result is False
    mock_client_cls.assert_not_called()
    mock_alert.assert_called_once()
    assert (
        mock_alert.call_args.kwargs["subject"]
        == "Portfonia ops: portfolio overview has no verified recipient"
    )


@patch(
    "app.services.email_sender.recipient_email_with_purpose",
    return_value=("test@example.com", "account_email"),
)
@patch("app.services.email_sender.get_settings")
@patch("app.services.email_sender.httpx.Client")
def test_send_http_error_returns_false(
    mock_client_cls: MagicMock,
    mock_settings: MagicMock,
    _recipient: MagicMock,
    db_session: Session,
) -> None:
    import httpx

    mock_settings.return_value = _mock_settings()
    db_session.add(_priced_stock("Apple", "AAPL", "150", "10", position=0))
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "boom", request=MagicMock(), response=MagicMock(status_code=500, text="err")
    )
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    mock_client_cls.return_value.__enter__.return_value = mock_client

    result = send_portfolio_overview_email(db_session, _USER_ID, "USD")

    assert result is False
