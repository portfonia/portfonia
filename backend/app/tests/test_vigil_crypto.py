"""services/vigil/crypto.py — dedicated Vigil data-key contextual Fernet
(issue #454, Vigil R0 P2.1).

Per Vigil Concept & Design.md appendix C.2, this is a fresh module using its
own key family (VIGIL_ENCRYPTION_KEY/_PREV) and its own contextual-payload
scheme — it must NOT reuse app.core.encryption.encrypt_value (that always
selects HOLDINGS_ENCRYPTION_KEY). Contract: #450 Design section 4 / 5 —
each encrypted value is JSON {version,purpose,table,row_id,vault_id,value};
verified after decrypt to reject cross-row ciphertext swaps (Fernet has no
AAD parameter).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

import pytest
from cryptography.fernet import Fernet

from app.services.vigil.crypto import VigilCryptoError, decrypt_field, encrypt_field

_VAULT_ID = uuid.uuid4()
_ROW_ID = uuid.uuid4()
_OTHER_VAULT_ID = uuid.uuid4()
_OTHER_ROW_ID = uuid.uuid4()


@dataclass(frozen=True)
class _Ctx:
    purpose: str = "vigil_configuration"
    table: str = "vigil_configurations"
    row_id: uuid.UUID = _ROW_ID
    vault_id: uuid.UUID = _VAULT_ID


def _enc(value: str, ctx: _Ctx = _Ctx()) -> str:
    return encrypt_field(
        value, purpose=ctx.purpose, table=ctx.table, row_id=ctx.row_id, vault_id=ctx.vault_id
    )


def _dec(token: str, ctx: _Ctx = _Ctx()) -> str:
    return decrypt_field(
        token, purpose=ctx.purpose, table=ctx.table, row_id=ctx.row_id, vault_id=ctx.vault_id
    )


@pytest.fixture(autouse=True)
def _vigil_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("VIGIL_ENCRYPTION_KEY_PREV", raising=False)
    get_settings.cache_clear()


def test_round_trip_returns_original_value() -> None:
    token = _enc("hello world")
    assert _dec(token) == "hello world"


def test_token_is_not_plaintext() -> None:
    token = _enc("secret-value")
    assert "secret-value" not in token


def test_decrypt_rejects_wrong_vault_id_context() -> None:
    token = _enc("hello")
    with pytest.raises(VigilCryptoError):
        _dec(token, replace(_Ctx(), vault_id=_OTHER_VAULT_ID))


def test_decrypt_rejects_wrong_row_id_context() -> None:
    token = _enc("hello")
    with pytest.raises(VigilCryptoError):
        _dec(token, replace(_Ctx(), row_id=_OTHER_ROW_ID))


def test_decrypt_rejects_wrong_table_context() -> None:
    token = _enc("hello")
    with pytest.raises(VigilCryptoError):
        _dec(token, replace(_Ctx(), table="vigil_objects"))


def test_decrypt_rejects_wrong_purpose_context() -> None:
    token = _enc("hello")
    with pytest.raises(VigilCryptoError):
        _dec(token, replace(_Ctx(), purpose="vigil_object_filename"))


def test_decrypt_rejects_token_from_different_key() -> None:
    """Simulates a copied ciphertext row: only the dedicated Vigil data key
    can decrypt it (A02/A04) — a wrong key must not silently succeed."""
    other_key = Fernet.generate_key().decode()
    token = _enc("hello")

    # Re-key the process to something else entirely and retry decrypt.
    import os

    from app.core.config import get_settings

    os.environ["VIGIL_ENCRYPTION_KEY"] = other_key
    get_settings.cache_clear()
    try:
        with pytest.raises(VigilCryptoError):
            _dec(token)
    finally:
        get_settings.cache_clear()


def test_holdings_key_alone_cannot_decrypt_vigil_ciphertext() -> None:
    """A04: holdings key is a completely independent key family."""
    from app.core.config import get_settings
    from app.core.encryption import encrypt_value

    holdings_ciphertext = encrypt_value("not a vigil payload")
    with pytest.raises(VigilCryptoError):
        _dec(holdings_ciphertext)
    get_settings.cache_clear()


def test_prev_key_still_decrypts_after_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    old_key = Fernet.generate_key().decode()
    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", old_key)
    monkeypatch.delenv("VIGIL_ENCRYPTION_KEY_PREV", raising=False)
    get_settings.cache_clear()
    token = _enc("still readable")

    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("VIGIL_ENCRYPTION_KEY_PREV", old_key)
    get_settings.cache_clear()

    assert _dec(token) == "still readable"


def test_missing_key_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.delenv("VIGIL_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("VIGIL_ENCRYPTION_KEY_PREV", raising=False)
    get_settings.cache_clear()
    with pytest.raises(VigilCryptoError):
        _enc("x")
    get_settings.cache_clear()


def test_decrypt_rejects_unknown_version() -> None:
    """Strict versioned payload: an unrecognized version fails rather than
    being silently accepted (#450 Design section 4)."""
    from app.services.vigil import crypto as crypto_module

    monkey_token = crypto_module._encrypt_envelope(
        {
            "version": 2,
            "purpose": "vigil_configuration",
            "table": "vigil_configurations",
            "row_id": str(_ROW_ID),
            "vault_id": str(_VAULT_ID),
            "value": "x",
        },
        crypto_module._build_fernet(),
    )
    with pytest.raises(VigilCryptoError):
        _dec(monkey_token)


def test_decrypt_rejects_unknown_extra_field() -> None:
    from app.services.vigil import crypto as crypto_module

    monkey_token = crypto_module._encrypt_envelope(
        {
            "version": 1,
            "purpose": "vigil_configuration",
            "table": "vigil_configurations",
            "row_id": str(_ROW_ID),
            "vault_id": str(_VAULT_ID),
            "value": "x",
            "extra": "unexpected",
        },
        crypto_module._build_fernet(),
    )
    with pytest.raises(VigilCryptoError):
        _dec(monkey_token)
