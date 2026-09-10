"""Tests for the snapshot outbox payload codec (issue #373).

Pure (no DB): the codec is what makes "replay the frozen payload" reproduce
the live rows byte-for-byte, so it is tested against a row built by the real
`build_snapshot_row` rather than a hand-written dict that could drift from it.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.portfolio_snapshot_outbox import PortfolioSnapshotOutbox
from app.services.portfolio_history import build_snapshot_row
from app.services.snapshot_outbox import (
    OUTBOX_APPLIED_RETENTION_DAYS,
    OutboxPayloadError,
    decode_payload,
    encode_payload,
    prune_applied_outbox,
)
from app.tests.conftest import seed_user

TODAY = date(2026, 9, 5)
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
HOLDING_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")

_PRICE_DATE = date(2026, 9, 4)
_FX_DATE = date(2026, 9, 3)


def _auto_row(**overrides: Any) -> dict[str, object]:
    """A fully-populated auto-priced row (every column non-None that can be)."""
    holding = Holding(
        user_id=USER_ID,
        name="Apple",
        ticker="AAPL",
        currency="CNY",
        pricing_mode="auto",
        shares=Decimal("10.50"),
        market="US",
        broker="Futu",
        account="A-1",
        portfolio="Core",
        asset_class="STOCK",
        capture_supported=True,
    )
    holding.id = HOLDING_ID
    kwargs: dict[str, Any] = {
        "h": holding,
        "user_id": USER_ID,
        "snapshot_date": TODAY,
        "base_currency": "USD",
        "fx_rates": {"USDCNY": (Decimal("7.0812"), _FX_DATE)},
        "price_lookup": lambda _key, _d: (Decimal("123.45"), _PRICE_DATE),
        "is_backfilled": False,
    }
    kwargs.update(overrides)
    return build_snapshot_row(**kwargs)


def test_round_trip_reproduces_the_row_exactly() -> None:
    row = _auto_row()

    payload, checksum = encode_payload([row])
    decoded = decode_payload(payload, checksum)

    assert decoded == [row]


def test_round_trip_preserves_types_not_just_values() -> None:
    decoded = decode_payload(*encode_payload([_auto_row()]))[0]

    assert isinstance(decoded["holding_id"], uuid.UUID)
    assert isinstance(decoded["snapshot_date"], date)
    assert isinstance(decoded["price_as_of"], date)
    assert isinstance(decoded["shares"], Decimal)
    assert isinstance(decoded["market_value_base"], Decimal)
    assert isinstance(decoded["capture_supported"], bool)
    assert isinstance(decoded["is_backfilled"], bool)
    assert decoded["ticker"] == "AAPL"


def test_round_trip_keeps_nulls_as_nulls() -> None:
    row = _auto_row(is_backfilled=True)
    row["price_as_of"] = None
    row["fx_rate_used"] = None
    row["fund_code"] = None

    decoded = decode_payload(*encode_payload([row]))[0]

    assert decoded["price_as_of"] is None
    assert decoded["fx_rate_used"] is None
    assert decoded["fund_code"] is None


def test_cash_row_round_trips() -> None:
    holding = Holding(
        user_id=USER_ID,
        name="Cash USD",
        currency="USD",
        pricing_mode="manual",
        current_value=Decimal("2500.00"),
        capture_supported=True,
    )
    holding.id = HOLDING_ID
    row = build_snapshot_row(
        holding,
        USER_ID,
        TODAY,
        "USD",
        {},
        lambda _key, _d: None,
        is_backfilled=False,
    )

    decoded = decode_payload(*encode_payload([row]))[0]

    assert decoded == row
    assert decoded["current_value"] == Decimal("2500.00")
    assert decoded["market_value"] is None


def test_empty_payload_round_trips() -> None:
    """Empty book: the exit day still has an intended payload (zero rows)."""
    assert decode_payload(*encode_payload([])) == []


def test_checksum_mismatch_is_rejected() -> None:
    payload, _checksum = encode_payload([_auto_row()])

    with pytest.raises(OutboxPayloadError):
        decode_payload(payload, "0" * 64)


def test_tampered_payload_is_rejected() -> None:
    payload, checksum = encode_payload([_auto_row()])
    tampered = payload.replace("AAPL", "MSFT")

    assert tampered != payload
    with pytest.raises(OutboxPayloadError):
        decode_payload(tampered, checksum)


def test_non_json_payload_is_rejected() -> None:
    with pytest.raises(OutboxPayloadError):
        decode_payload("not json", "0" * 64)


def test_payload_shape_mismatch_is_rejected() -> None:
    with pytest.raises(OutboxPayloadError):
        decode_payload('{"v": 1, "rows": {"not": "a list"}}', "0" * 64)


def test_unsupported_value_type_is_rejected_on_encode() -> None:
    row = _auto_row()
    row["asset_class"] = {"nested": "dict"}

    with pytest.raises(OutboxPayloadError):
        encode_payload([row])


def test_prune_applied_outbox_keeps_pending_evidence(db_session: Session) -> None:
    """Retention may drop published rows past the window; anything still
    carrying recovery value must survive."""
    user_id = uuid.uuid4()
    seed_user(db_session, user_id)
    today = date(2026, 9, 9)
    old = today - timedelta(days=OUTBOX_APPLIED_RETENTION_DAYS + 1)
    recent = today - timedelta(days=1)

    for day, status in (
        (old, "applied"),
        (old + timedelta(days=1), "computed"),
        (old + timedelta(days=2), "failed"),
        (recent, "applied"),
    ):
        db_session.add(
            PortfolioSnapshotOutbox(
                user_id=user_id,
                snapshot_date=day,
                status=status,
                payload="irrelevant-for-retention",
                checksum="irrelevant-for-retention",
            )
        )
    db_session.flush()

    pruned = prune_applied_outbox(db_session, today=today)

    assert pruned == 1
    remaining = {
        (r.snapshot_date, r.status)
        for r in db_session.execute(
            select(PortfolioSnapshotOutbox).where(PortfolioSnapshotOutbox.user_id == user_id)
        ).scalars()
    }
    assert remaining == {
        (old + timedelta(days=1), "computed"),
        (old + timedelta(days=2), "failed"),
        (recent, "applied"),
    }
