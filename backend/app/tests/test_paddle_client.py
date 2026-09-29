"""Paddle signature and configuration boundaries."""

import hashlib
import hmac

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings
from app.services.paddle_client import verify_signature


def _settings_input() -> dict[str, object]:
    values: dict[str, object] = {
        key: "test" for key, field in Settings.model_fields.items() if field.is_required()
    }
    values.update(
        {
            "APP_BASE_URL": "https://example.com",
            "FRONTEND_URL": "https://example.com",
            "HOLDINGS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "ADMIN_API_TOKEN": "test-token",
            "SUPABASE_URL": "https://example.supabase.co",
            "PADDLE_ENVIRONMENT": "production",
        }
    )
    return values


@pytest.mark.parametrize(
    "field,value",
    [("PADDLE_API_KEY", "pdl_sdbx_FAKE_TEST"), ("PADDLE_CLIENT_SIDE_TOKEN", "test_FAKE_TEST")],
)
def test_environment_rejects_mismatched_credentials_without_exposing_values(
    field: str, value: str
) -> None:
    values = _settings_input()
    values[field] = value
    with pytest.raises(ValidationError) as error:
        Settings.model_validate(values)
    assert field in str(error.value)
    assert value not in str(error.value)


@pytest.mark.parametrize("amount", ["0", "1.234"])
def test_pack_amount_must_be_positive_with_two_decimals(amount: str) -> None:
    values = _settings_input()
    values["PADDLE_CREDIT_PACKS"] = {"pri_test": amount}
    with pytest.raises(ValidationError, match="PADDLE_CREDIT_PACKS"):
        Settings.model_validate(values)


def test_signature_accepts_second_valid_digest_and_rejects_tampering() -> None:
    raw = b'{"event_type":"transaction.completed"}'
    digest = hmac.new(b"test_secret", b"123:" + raw, hashlib.sha256).hexdigest()
    header = f"ts=123;h1={'0' * 64};h1={digest}"
    assert verify_signature(raw, header, "test_secret")
    assert not verify_signature(raw + b" ", header, "test_secret")
    assert not verify_signature(raw, None, "test_secret")
