"""Thirty-day signed revoke-all links, using the unsubscribe encoding."""

from datetime import timedelta
from uuid import UUID

from app.services import api_tokens
from app.services.unsubscribe_token import _decode, _encode, _sign

PREFIX = "api-token-revoke-v1"


def create_link(user_id: UUID) -> str:
    expires = api_tokens.now_et() + timedelta(days=30)
    payload = f"{PREFIX}:{user_id}:{int(expires.timestamp())}"
    return _encode(payload, _sign(payload))


def verify_link(token: str) -> UUID | None:
    import hmac

    try:
        decoded = _decode(token)
        if decoded is None:
            return None
        payload, digest = decoded
        if not hmac.compare_digest(digest, _sign(payload)):
            return None
        prefix, user_id, expires = payload.split(":")
        if prefix != PREFIX or api_tokens.now_et().timestamp() > int(expires):
            return None
        return UUID(user_id)
    except Exception:
        return None
