"""Authenticated purchase configuration for Paddle.js."""

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import get_settings
from app.core.deps import Principal, current_principal

router = APIRouter()


@router.get("/checkout-config")
def checkout_config(principal: Principal = Depends(current_principal)) -> dict[str, object]:
    settings = get_settings()
    if not (
        settings.PADDLE_ENVIRONMENT
        and settings.PADDLE_CLIENT_SIDE_TOKEN
        and settings.PADDLE_CREDIT_PACKS
    ):
        raise HTTPException(status_code=503, detail="payments not configured")
    packs = sorted(settings.PADDLE_CREDIT_PACKS.items(), key=lambda item: item[1])
    return {
        "environment": settings.PADDLE_ENVIRONMENT,
        "client_token": settings.PADDLE_CLIENT_SIDE_TOKEN,
        "user_id": str(principal.user_id),
        "email": principal.email,
        "packs": [
            {"price_id": price_id, "credits": f"{credits:.2f}"} for price_id, credits in packs
        ],
    }
