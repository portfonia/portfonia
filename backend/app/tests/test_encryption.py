"""Unit + integration coverage for holdings field-level encryption (issue #31)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.encryption import (
    EncryptedDecimal,
    EncryptedString,
    HoldingsDecryptionError,
    decrypt_value,
    encrypt_value,
)
from app.models.holding import Holding
from app.tests.conftest import TEST_USER_ID


def test_encrypted_string_round_trips() -> None:
    col = EncryptedString()
    token = col.process_bind_param("Apple Inc", dialect=None)
    assert token is not None
    assert token != "Apple Inc"
    assert col.process_result_value(token, dialect=None) == "Apple Inc"


def test_encrypted_string_null_passes_through_unencrypted() -> None:
    col = EncryptedString()
    assert col.process_bind_param(None, dialect=None) is None
    assert col.process_result_value(None, dialect=None) is None


def test_encrypted_decimal_round_trips() -> None:
    col = EncryptedDecimal()
    token = col.process_bind_param(Decimal("167.80"), dialect=None)
    assert token is not None
    assert col.process_result_value(token, dialect=None) == Decimal("167.80")


def test_encrypted_decimal_null_passes_through_unencrypted() -> None:
    col = EncryptedDecimal()
    assert col.process_bind_param(None, dialect=None) is None
    assert col.process_result_value(None, dialect=None) is None


def test_unicode_value_round_trips() -> None:
    # A real dev-DB holding name is Chinese ("工银黄金") — verified during the
    # migration dry run; keep a regression test for it here.
    token = encrypt_value("工银黄金")
    assert decrypt_value(token) == "工银黄金"


def test_key_rotation_decrypts_tokens_from_the_previous_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MultiFernet must decrypt with either configured key (rotation window).

    Also pins *which* key new writes use: `new_token != token` alone doesn't
    prove it (different plaintexts, or even the same plaintext through
    Fernet's random IV, always produce different tokens) — decrypt with each
    single key in isolation instead (PR #111 review).
    """
    old_key = get_settings().HOLDINGS_ENCRYPTION_KEY.get_secret_value()
    token = encrypt_value("pre-rotation value")

    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("HOLDINGS_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("HOLDINGS_ENCRYPTION_KEY_PREV", old_key)
    get_settings.cache_clear()
    try:
        assert decrypt_value(token) == "pre-rotation value"

        new_token = encrypt_value("post-rotation value")
        assert Fernet(new_key.encode()).decrypt(new_token.encode()) == b"post-rotation value"
        with pytest.raises(InvalidToken):
            Fernet(old_key.encode()).decrypt(new_token.encode())
    finally:
        get_settings.cache_clear()


def test_empty_prev_key_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank env value must not be handed to Fernet() as a real key."""
    monkeypatch.setenv("HOLDINGS_ENCRYPTION_KEY_PREV", "")
    get_settings.cache_clear()
    try:
        token = encrypt_value("still works")
        assert decrypt_value(token) == "still works"
    finally:
        get_settings.cache_clear()


def test_decrypt_with_wrong_key_raises_domain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key mismatch must fail as a clear, wrapped error — not a bare InvalidToken."""
    token = encrypt_value("encrypted under the real key")

    monkeypatch.setenv("HOLDINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    try:
        with pytest.raises(HoldingsDecryptionError) as exc_info:
            decrypt_value(token)
        assert token not in str(exc_info.value)
    finally:
        get_settings.cache_clear()


def test_empty_string_round_trips_and_is_not_null_in_db(db_session: Session) -> None:
    """Empty string is a real (if odd) value, distinct from NULL — must not collapse to NULL."""
    holding = Holding(
        user_id=TEST_USER_ID,
        name="Untitled",
        pricing_mode="manual",
        currency="USD",
        notes="",
        current_value=Decimal("1"),
        asset_class="CASH_EQUIV",
    )
    db_session.add(holding)
    db_session.commit()

    raw_notes = db_session.execute(
        text("SELECT notes FROM holdings WHERE id = :id"), {"id": holding.id}
    ).scalar_one()
    assert raw_notes is not None
    assert raw_notes != ""

    db_session.expire_all()
    reloaded = db_session.get(Holding, holding.id)
    assert reloaded is not None
    assert reloaded.notes == ""


def test_equality_filter_on_encrypted_column_raises(db_session: Session) -> None:
    """SQL-level `==`/`.in_()` on ciphertext silently matches nothing — must fail loud instead."""
    with pytest.raises(NotImplementedError):
        db_session.execute(select(Holding).where(Holding.ticker == "AAPL"))
    with pytest.raises(NotImplementedError):
        db_session.execute(select(Holding).where(Holding.ticker.in_(["AAPL", "MSFT"])))


def test_null_filters_on_encrypted_column_still_work(db_session: Session) -> None:
    """IS NULL / IS NOT NULL must keep working — that's the one SQL-level check real code uses."""
    db_session.execute(select(Holding).where(Holding.ticker.is_(None))).all()
    db_session.execute(select(Holding).where(Holding.ticker.isnot(None))).all()


def test_holding_stored_ciphertext_differs_from_plaintext_and_decrypts_via_orm(
    db_session: Session,
) -> None:
    """Prove the DB actually holds ciphertext, not just that the ORM round-trips."""
    holding = Holding(
        user_id=TEST_USER_ID,
        name="Apple Inc",
        pricing_mode="auto",
        ticker="AAPL",
        currency="USD",
        shares=Decimal("10"),
        avg_cost=Decimal("180.50"),
        asset_class="EQUITY_US_BROAD",
    )
    db_session.add(holding)
    db_session.commit()

    raw = db_session.execute(
        text("SELECT name, ticker, shares, avg_cost FROM holdings WHERE id = :id"),
        {"id": holding.id},
    ).one()
    raw_name, raw_ticker, raw_shares, raw_avg_cost = raw
    assert raw_name != "Apple Inc"
    assert raw_ticker != "AAPL"
    assert raw_shares != "10"
    assert raw_avg_cost != "180.50"

    db_session.expire_all()
    reloaded = db_session.get(Holding, holding.id)
    assert reloaded is not None
    assert reloaded.name == "Apple Inc"
    assert reloaded.ticker == "AAPL"
    assert reloaded.shares == Decimal("10")
    assert reloaded.avg_cost == Decimal("180.50")


def test_holding_null_fields_stay_null_in_db(db_session: Session) -> None:
    holding = Holding(
        user_id=TEST_USER_ID,
        name="USD Cash",
        pricing_mode="manual",
        currency="USD",
        current_value=Decimal("15000"),
        asset_class="CASH_EQUIV",
    )
    db_session.add(holding)
    db_session.commit()

    raw_ticker = db_session.execute(
        text("SELECT ticker FROM holdings WHERE id = :id"), {"id": holding.id}
    ).scalar_one()
    assert raw_ticker is None
