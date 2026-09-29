"""Small server-side Paddle API boundary for refunds."""

from __future__ import annotations

import hashlib
import hmac
from typing import cast

import httpx

from app.core.config import get_settings


class PaddleNotConfigured(Exception):
    """The payment environment or API key is unavailable."""


class PaddleApiError(Exception):
    def __init__(self, status: int, code: str | None, detail: str | None) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"Paddle API error ({status}, {code})")


def verify_signature(raw_body: bytes, header: str | None, secret: str) -> bool:
    if not header:
        return False
    parts = [part.strip().partition("=") for part in header.split(";")]
    timestamps = [value for key, separator, value in parts if key == "ts" and separator]
    digests = [value for key, separator, value in parts if key == "h1" and separator]
    if not timestamps or not digests:
        return False
    expected = hmac.new(
        secret.encode(), timestamps[0].encode() + b":" + raw_body, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, digest) for digest in digests)


def _base_url() -> str:
    settings = get_settings()
    if not settings.PADDLE_ENVIRONMENT or not settings.PADDLE_API_KEY:
        raise PaddleNotConfigured
    return (
        "https://api.paddle.com"
        if settings.PADDLE_ENVIRONMENT == "production"
        else "https://sandbox-api.paddle.com"
    )


def _request(method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    base_url = _base_url()
    key = get_settings().PADDLE_API_KEY
    assert key is not None
    with httpx.Client(timeout=15) as client:
        response = client.request(
            method,
            base_url + path,
            headers={"Authorization": f"Bearer {key.get_secret_value()}"},
            json=payload,
        )
    if not response.is_success:
        try:
            error = cast(dict[str, object], response.json()).get("error")
            details = error if isinstance(error, dict) else {}
        except ValueError:
            details = {}
        code = details.get("code")
        detail = details.get("detail")
        raise PaddleApiError(
            response.status_code,
            code if isinstance(code, str) else None,
            detail if isinstance(detail, str) else None,
        )
    result = cast(dict[str, object], response.json())
    data = result.get("data")
    if not isinstance(data, dict):
        raise ValueError("Paddle response has no data")
    return cast(dict[str, object], data)


def get_transaction(transaction_id: str) -> dict[str, object]:
    return _request("GET", f"/transactions/{transaction_id}")


def create_refund_adjustment(
    *, transaction_id: str, reason: str, full: bool, item_id: str | None, amount: str | None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "refund",
        "transaction_id": transaction_id,
        "reason": reason,
        "type": "full" if full else "partial",
    }
    if not full:
        payload["tax_mode"] = "internal"
        payload["items"] = [{"item_id": item_id, "type": "partial", "amount": amount}]
    return _request("POST", "/adjustments", payload)
