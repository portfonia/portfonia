"""Issue #584 transactional locale acceptance contracts; all delivery is mocked."""

import base64
import hashlib
import hmac
import re
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezones import ET
from app.models.waitlist_entry import WaitlistEntry
from app.routers.invitation_letters import _COPY
from app.services import email_sender
from app.services.invitation_unsubscribe import create_token, verify_token
from app.services.invites import create_invite
from app.services.zh_hant import to_traditional
from app.tests.conftest import seed_user


@pytest.mark.parametrize(
    "name",
    [
        "_VERIFICATION_EMAIL_COPY",
        "_PORTFOLIO_OVERVIEW_COPY",
        "_INVITATION_LETTER_COPY",
        "_UNSUBSCRIBE_FOOTER_COPY",
        "_COPY",
    ],
)
def test_locale_dict_consistency(name: str) -> None:
    copies = _COPY if name == "_COPY" else getattr(email_sender, name)
    assert set(copies) == {"en", "zh", "zh-Hant"}
    reference = copies["en"]
    for copy in copies.values():
        assert copy.keys() == reference.keys()
        for key, text in copy.items():
            assert set(re.findall(r"\{([^{}]+)\}", text)) == set(
                re.findall(r"\{([^{}]+)\}", reference[key])
            )


def test_verification_traditional_copy() -> None:
    with patch("app.services.email_sender.httpx.Client") as client:
        post = client.return_value.__enter__.return_value.post
        post.return_value.json.return_value = {"id": "mock-verification"}
        assert (
            email_sender.send_verification_email("test@example.com", "test-token", locale="zh-Hant")
            == "mock-verification"
        )
        copy = email_sender._VERIFICATION_EMAIL_COPY["zh-Hant"]
        payload = post.call_args.kwargs["json"]
        assert payload["subject"] == copy["subject"]
        assert payload["text"] == copy["body"].format(
            url=f"{get_settings().FRONTEND_URL}/verify-email?token=test-token"
        )


def test_overview_traditional_copy(db_session: Session) -> None:
    user = seed_user(db_session, uuid.uuid4(), email="overview@example.com")
    assert user is not None
    user.locale = "zh-Hant"
    user.email_verified_at = datetime.now(tz=ET)
    db_session.flush()
    with patch("app.services.email_sender.httpx.Client") as client:
        assert email_sender.send_portfolio_overview_email(db_session, user.id, "USD")
        payload = client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
    copy = email_sender._PORTFOLIO_OVERVIEW_COPY["zh-Hant"]
    assert payload["subject"] == copy["subject"]
    for key in (
        "intro",
        "col_holding",
        "col_currency",
        "col_value",
        "col_pct",
        "col_custodian",
        "col_asset_class",
        "total_label",
        "next_report_label",
    ):
        assert copy[key] in payload["text"]
    assert copy["priced_note"].format(n=0, priced=0, pending=0) in payload["text"]
    assert to_traditional(payload["text"]) == payload["text"]
    assert "不構成" in payload["text"]
    assert email_sender._glossary_term("STOCK", "zh-Hant") == to_traditional(
        email_sender._glossary_term("STOCK", "zh")
    )
    assert email_sender._glossary_term("Portfonia Holdings Briefing", "zh-Hant") == to_traditional(
        email_sender._glossary_term("Portfonia Holdings Briefing", "zh")
    )


def test_invitation_traditional_copy() -> None:
    with patch("app.services.email_sender.httpx.Client") as client:
        post = client.return_value.__enter__.return_value.post
        post.return_value.json.return_value = {"id": "mock-invitation"}
        assert (
            email_sender.send_invitation_letter(
                "test@example.com",
                "https://example.com/signup?lang=zh-Hant",
                "https://example.com/unsubscribe",
                locale="zh-Hant",
                idempotency_key="mock-key",
            )
            == "mock-invitation"
        )
        payload = post.call_args.kwargs["json"]
        copy = email_sender._INVITATION_LETTER_COPY["zh-Hant"]
        assert payload["subject"] == copy["subject"]
        for index in range(1, 5):
            assert copy[f"paragraph{index}"] in payload["text"]
            assert copy[f"paragraph{index}"] in payload["html"]
        assert (
            copy["footer"].format(unsubscribe_url="https://example.com/unsubscribe")
            in payload["text"]
        )


@pytest.mark.parametrize(
    ("ui_locale", "language"), [("zh-Hant", "zh-Hant"), ("zh-Hans", "zh"), ("en", "en")]
)
def test_waitlist_invitation_locale(
    app_client: TestClient, db_session: Session, ui_locale: str, language: str
) -> None:
    email = f"{ui_locale.lower()}@example.com"
    db_session.add(WaitlistEntry(email=email, locale=ui_locale, status="pending"))
    db_session.commit()
    with (
        patch(
            "app.services.invitation_letters.send_invitation_letter", return_value="mock-id"
        ) as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        response = app_client.post(
            "/admin/invitation-letters",
            json={"email": email},
            headers={
                "Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"
            },
        )
    assert response.status_code == 201
    assert response.json()["language"] == language
    assert f"lang={ui_locale}" in response.json()["invite_url"]
    assert send.call_args.kwargs["locale"] == language
    token = send.call_args.args[2].split("token=", 1)[1]
    assert verify_token(token) == (uuid.UUID(response.json()["invite_id"]), language)


def test_explicit_invitation_language(app_client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {get_settings().ADMIN_API_TOKEN.get_secret_value()}"}
    with (
        patch(
            "app.services.invitation_letters.send_invitation_letter", return_value="mock-id"
        ) as send,
        patch("app.services.invitation_letters.poll_invitation_letter_delivery.apply_async"),
    ):
        response = app_client.post(
            "/admin/invitation-letters",
            json={"email": "direct@example.com", "language": "zh-Hant"},
            headers=headers,
        )
        assert response.status_code == 201
        assert "lang=zh-Hant" in response.json()["invite_url"]
        assert send.call_args.kwargs["locale"] == "zh-Hant"
        send.reset_mock()
        assert (
            app_client.post(
                "/admin/invitation-letters",
                json={"email": "invalid@example.com", "language": "fr"},
                headers=headers,
            ).status_code
            == 422
        )
        send.assert_not_called()


def test_unsubscribe_tokens_and_page(app_client: TestClient, db_session: Session) -> None:
    invite = create_invite(db_session, created_by=uuid.uuid4(), email="unsubscribe@example.com")
    db_session.commit()
    # Independently reproduce the pre-584 payload and signing algorithm.
    payload = f"invitation-unsubscribe-v1:{invite.id}:zh"
    digest = hmac.new(
        get_settings().APP_SECRET_KEY.get_secret_value().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    old_token = base64.urlsafe_b64encode(f"{payload}.{digest}".encode()).decode().rstrip("=")
    assert create_token(invite.id, "zh") == old_token
    assert verify_token(old_token) == (invite.id, "zh")
    assert verify_token(create_token(invite.id, "fr")) is None
    token = create_token(invite.id, "zh-Hant")
    assert verify_token(token) == (invite.id, "zh-Hant")
    response = app_client.get("/invitation-letters/unsubscribe", params={"token": token})
    assert response.status_code == 200
    assert _COPY["zh-Hant"]["confirm"] in response.text
    assert _COPY["zh-Hant"]["button"] in response.text
    response = app_client.post("/invitation-letters/unsubscribe", params={"token": token})
    assert response.status_code == 200
    assert _COPY["zh-Hant"]["success"] in response.text
