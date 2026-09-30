"""Signed, non-expiring unsubscribe links for invitation letters."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from uuid import UUID

from app.core.config import get_settings

_PREFIX = "invitation-unsubscribe-v1"


def create_token(invite_id: UUID, locale: str) -> str:
    payload = f"{_PREFIX}:{invite_id}:{locale}"
    digest = hmac.new(
        get_settings().APP_SECRET_KEY.get_secret_value().encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{digest}".encode()).decode().rstrip("=")


def verify_token(token: str) -> tuple[UUID, str] | None:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        payload, digest = raw.rsplit(".", 1)
        expected = hmac.new(
            get_settings().APP_SECRET_KEY.get_secret_value().encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(digest, expected):
            return None
        prefix, invite_id, locale = payload.split(":")
        if prefix != _PREFIX or locale not in ("en", "zh", "zh-Hant"):
            return None
        return UUID(invite_id), locale
    except (ValueError, UnicodeError, TypeError, binascii.Error):
        return None
