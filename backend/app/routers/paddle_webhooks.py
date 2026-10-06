"""Signed Paddle notifications for purchases and refund adjustments."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.timezones import today_et
from app.models.credit_ledger import CreditLedgerEntry
from app.models.user import User
from app.services.credit_ledger import (
    IdempotencyConflict,
    record_purchase,
    referral_recharge_bonus,
    reverse_referral_clawback,
    reverse_refund,
)
from app.services.email_sender import send_ops_alert
from app.services.paddle_client import verify_signature
from app.services.subscription import maybe_send_low_balance_reminder

router = APIRouter()


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


async def _raw_body(request: Request) -> bytes:
    return await request.body()


# Sync handler (FastAPI runs it in the threadpool): the body does blocking DB
# work and sync httpx alert sends that must not stall the event loop. The raw
# bytes for signature verification come from the async dependency above.
@router.post("/paddle")
def paddle_webhook(
    request: Request,
    raw: bytes = Depends(_raw_body),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    settings = get_settings()
    secret = settings.PADDLE_WEBHOOK_SECRET
    if secret is None:
        send_ops_alert(
            "Paddle webhook secret not configured",
            "Paddle webhook cannot be verified",
            idempotency_key=f"paddle-webhook-unconfigured:{today_et()}",
        )
        raise HTTPException(status_code=503, detail="payments not configured")
    if settings.PADDLE_ENVIRONMENT is None:
        raise HTTPException(status_code=503, detail="payments not configured")
    if not verify_signature(
        raw, request.headers.get("Paddle-Signature"), secret.get_secret_value()
    ):
        raise HTTPException(status_code=401, detail="invalid signature")
    event = _object(json.loads(raw))
    data = _object(event.get("data"))
    event_type = event.get("event_type")
    if event_type == "transaction.completed" and data.get("status") == "completed":
        txn = _string(data.get("id"))
        custom = _object(data.get("custom_data"))
        user_id_value = custom.get("user_id")
        user_id: UUID | None = None
        try:
            if isinstance(user_id_value, str):
                user_id = UUID(user_id_value)
        except ValueError:
            pass
        details = _object(data.get("details"))
        totals = _object(details.get("totals"))
        if user_id is None or session.get(User, user_id) is None:
            send_ops_alert(
                "Paddle transaction has no matching user",
                f"transaction={txn} customer={data.get('customer_id')} currency={data.get('currency_code')} total={totals.get('total')} reason=missing user",
                idempotency_key=f"paddle-txn-unmatched:{txn}",
            )
            return {"status": "ok"}
        credits = Decimal("0")
        unknown: list[str] = []
        items = data.get("items")
        for item_value in items if isinstance(items, list) else []:
            item = _object(item_value)
            price_id = _string(_object(item.get("price")).get("id"))
            pack_credits = get_settings().PADDLE_CREDIT_PACKS.get(price_id)
            if pack_credits is None:
                unknown.append(price_id)
            else:
                credits += pack_credits * Decimal(str(item.get("quantity")))
        if unknown:
            send_ops_alert(
                "Paddle transaction contains unknown price",
                f"transaction={txn} price_ids={unknown}",
                idempotency_key=f"paddle-txn-unknown-price:{txn}",
            )
        if credits == 0:
            return {"status": "ok"}
        try:
            purchase = record_purchase(
                session,
                user_id=user_id,
                credits=credits,
                transaction_id=txn,
                note=f"{data.get('currency_code')} {totals.get('total')}",
            )
            if not purchase.replayed:
                referral_recharge_bonus(session, user_id, txn, credits)
            session.commit()
            if not purchase.replayed:
                maybe_send_low_balance_reminder(session, user_id)
        except IdempotencyConflict:
            session.rollback()
            send_ops_alert(
                "Paddle transaction credit conflict",
                f"transaction={txn}",
                idempotency_key=f"paddle-txn-conflict:{txn}",
            )
        except Exception:
            session.rollback()
            raise
    elif event_type == "adjustment.created":
        adjustment_id = _string(data.get("id"))
        if not _string(data.get("reason")).startswith("portfonia-refund:"):
            send_ops_alert(
                "Paddle adjustment not created by Portfonia",
                f"id={adjustment_id} action={data.get('action')} transaction={data.get('transaction_id')} status={data.get('status')} totals={data.get('totals')}",
                idempotency_key=f"paddle-adjustment-external:{adjustment_id}",
            )
    elif event_type == "adjustment.updated" and data.get("status") == "rejected":
        adjustment_id = _string(data.get("id"))
        write = reverse_refund(session, adjustment_id=adjustment_id)
        if write and not write.replayed:
            debit = session.scalar(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.reason == "refund",
                    CreditLedgerEntry.amount < 0,
                    CreditLedgerEntry.reference == adjustment_id,
                )
            )
            assert debit is not None
            transaction_id = debit.idempotency_key.split(":", 2)[1]
            try:
                reverse_referral_clawback(
                    session, debit.idempotency_key, transaction_id, adjustment_id
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            send_ops_alert(
                "Paddle rejected refund; credits restored",
                f"adjustment={adjustment_id}",
                idempotency_key=f"paddle-refund-rejected:{adjustment_id}",
                severity="INFO",
            )
    return {"status": "ok"}
