from uuid import UUID

from app.core.config import get_settings


def get_current_user_id() -> UUID:
    """Ring 0: fixed dev user. Swap this for JWT extraction in MVP."""
    return UUID(get_settings().DEV_USER_ID)
