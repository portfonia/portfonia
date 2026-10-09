"""Jade A7 notice names and Profile page references."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from app.services import email_sender as email


@pytest.mark.parametrize(
    "locale,name,new,old",
    [
        ("en", "Jade", "Profile page", "unused"),
        ("zh", "\u6da6\u7389", "\u4e2a\u4eba\u4e2d\u5fc3\u9875\u9762", "\u4e2a\u4eba\u8d44\u6599"),
        (
            "zh-Hant",
            "\u6f64\u7389",
            "\u500b\u4eba\u4e2d\u5fc3\u9801\u9762",
            "\u500b\u4eba\u8cc7\u6599",
        ),
    ],
)
@pytest.mark.parametrize("kind", ["low_balance", "expired"])
def test_a7_notice(locale: str, name: str, new: str, old: str, kind: str) -> None:
    with patch.object(httpx, "Client") as client:
        post = client.return_value.__enter__.return_value.post
        post.return_value.json.return_value = {"id": "jade-notice"}
        assert (
            email.send_subscription_notice(
                "jade@example.com",
                kind,
                locale=locale,
                plan="jade",
                expires_on=date(2026, 11, 16),
                fee=Decimal("9.99"),
                balance=Decimal("0.01"),
            )
            == "jade-notice"
        )
        text = post.call_args.kwargs["json"]["text"]
    assert name in text and new in text and old not in text
    assert "9.99" in text and "0.01" in text and "2026-11-16" in text
