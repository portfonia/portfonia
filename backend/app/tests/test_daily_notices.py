"""Issue #650 acceptance 13: Daily plan name in each notice locale."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from app.services import email_sender as email


@pytest.mark.parametrize(
    "locale,plan_name", [("en", "Daily"), ("zh", "\u6bcf\u65e5"), ("zh-Hant", "\u6bcf\u65e5")]
)
@pytest.mark.parametrize("kind", ["low_balance", "expired"])
def test_daily_acceptance_13_notice_names(locale: str, plan_name: str, kind: str) -> None:
    with patch.object(httpx, "Client") as client:
        response = client.return_value.__enter__.return_value.post.return_value
        response.json.return_value = {"id": "daily-notice"}
        assert (
            email.send_subscription_notice(
                "daily@example.com",
                kind,
                locale=locale,
                plan="daily",
                expires_on=date(2026, 11, 7),
                fee=Decimal("2.49"),
                balance=Decimal("0.50"),
            )
            == "daily-notice"
        )
        payload = client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
    assert plan_name in payload["text"]
    assert "2.49" in payload["text"] and "0.50" in payload["text"]
    assert "2026-11-07" in payload["text"]
