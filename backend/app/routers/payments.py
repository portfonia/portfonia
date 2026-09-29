"""Authenticated purchase configuration and crediting status for Paddle.js."""

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.deps import Principal, current_principal
from app.models.credit_ledger import CreditLedgerEntry
from app.services.credit_ledger import purchase_key

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


@router.get("/purchases/{transaction_id}")
def purchase_status(
    transaction_id: str = Path(pattern=r"^txn_"),
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    row = session.scalar(
        select(CreditLedgerEntry).where(
            CreditLedgerEntry.idempotency_key == purchase_key(transaction_id),
            CreditLedgerEntry.user_id == principal.user_id,
        )
    )
    if row is None:
        return {"transaction_id": transaction_id, "credited": False, "credits": None}
    return {
        "transaction_id": transaction_id,
        "credited": True,
        "credits": f"{row.amount:.2f}",
    }
