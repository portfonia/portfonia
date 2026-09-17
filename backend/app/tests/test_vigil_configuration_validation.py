"""services/vigil/configuration.py — pure input validation/normalization
(no DB, no network). Persistence + DNS + locking are covered by
test_vigil_configuration_service.py / test_vigil_router_configurations.py.

Contract: #450 Design section 5 (email normalization, interval/grace/
recipients/message bounds) and Appendix A (configurations.data_cipher
business JSON shape).
"""

from __future__ import annotations

import pytest

from app.services.vigil.configuration import (
    VigilConfigurationInputError,
    normalize_email,
    validate_configuration_input,
)


def test_normalize_email_trims_and_lowercases_domain() -> None:
    assert normalize_email("  Alice@Example.COM  ") == "Alice@example.com"


def test_normalize_email_preserves_local_part_case() -> None:
    assert normalize_email("MixedCase@example.com") == "MixedCase@example.com"


def test_normalize_email_rejects_missing_at() -> None:
    with pytest.raises(ValueError):
        normalize_email("not-an-email")


def test_normalize_email_rejects_empty_local_part() -> None:
    with pytest.raises(ValueError):
        normalize_email("@example.com")


def test_normalize_email_rejects_empty_domain() -> None:
    with pytest.raises(ValueError):
        normalize_email("alice@")


def test_normalize_email_idna_encodes_unicode_domain() -> None:
    result = normalize_email("alice@例え.jp")
    assert result.startswith("alice@xn--")


def _recipients(*emails: str) -> list[dict[str, str]]:
    return [{"email": e, "email_confirm": e} for e in emails]


def test_validate_configuration_input_happy_path() -> None:
    normalized = validate_configuration_input(
        interval_days=30,
        grace_hours=72,
        recipients=_recipients("a@example.com", "b@example.com"),
        message="hello",
    )
    assert normalized.interval_days == 30
    assert normalized.grace_hours == 72
    assert [r.email for r in normalized.recipients] == ["a@example.com", "b@example.com"]
    assert normalized.message == "hello"


def test_validate_configuration_input_defaults() -> None:
    normalized = validate_configuration_input(
        interval_days=None,
        grace_hours=None,
        recipients=_recipients("a@example.com"),
        message=None,
    )
    assert normalized.interval_days == 30
    assert normalized.grace_hours == 72
    assert normalized.message == ""


@pytest.mark.parametrize("interval_days", [1, 8, 15, 100, 0, -30])
def test_validate_configuration_input_rejects_bad_interval(interval_days: int) -> None:
    with pytest.raises(VigilConfigurationInputError):
        validate_configuration_input(
            interval_days=interval_days,
            grace_hours=72,
            recipients=_recipients("a@example.com"),
            message="",
        )


@pytest.mark.parametrize("interval_days", [7, 14, 30, 60, 90])
def test_validate_configuration_input_accepts_all_valid_intervals(interval_days: int) -> None:
    validate_configuration_input(
        interval_days=interval_days,
        grace_hours=72,
        recipients=_recipients("a@example.com"),
        message="",
    )


@pytest.mark.parametrize("grace_hours", [23, 169, 0, -1])
def test_validate_configuration_input_rejects_bad_grace(grace_hours: int) -> None:
    with pytest.raises(VigilConfigurationInputError):
        validate_configuration_input(
            interval_days=30,
            grace_hours=grace_hours,
            recipients=_recipients("a@example.com"),
            message="",
        )


@pytest.mark.parametrize("grace_hours", [24, 72, 168])
def test_validate_configuration_input_accepts_grace_bounds(grace_hours: int) -> None:
    validate_configuration_input(
        interval_days=30,
        grace_hours=grace_hours,
        recipients=_recipients("a@example.com"),
        message="",
    )


def test_validate_configuration_input_rejects_zero_recipients() -> None:
    with pytest.raises(VigilConfigurationInputError):
        validate_configuration_input(interval_days=30, grace_hours=72, recipients=[], message="")


def test_validate_configuration_input_rejects_more_than_three_recipients() -> None:
    with pytest.raises(VigilConfigurationInputError):
        validate_configuration_input(
            interval_days=30,
            grace_hours=72,
            recipients=_recipients("a@x.com", "b@x.com", "c@x.com", "d@x.com"),
            message="",
        )


def test_validate_configuration_input_accepts_three_recipients() -> None:
    normalized = validate_configuration_input(
        interval_days=30,
        grace_hours=72,
        recipients=_recipients("a@x.com", "b@x.com", "c@x.com"),
        message="",
    )
    assert [r.position for r in normalized.recipients] == [1, 2, 3]


def test_validate_configuration_input_rejects_duplicate_recipients() -> None:
    with pytest.raises(VigilConfigurationInputError):
        validate_configuration_input(
            interval_days=30,
            grace_hours=72,
            recipients=_recipients("a@example.com", "A@Example.com"),
            message="",
        )


def test_validate_configuration_input_rejects_email_confirm_mismatch() -> None:
    with pytest.raises(VigilConfigurationInputError):
        validate_configuration_input(
            interval_days=30,
            grace_hours=72,
            recipients=[{"email": "a@example.com", "email_confirm": "b@example.com"}],
            message="",
        )


def test_validate_configuration_input_rejects_malformed_email() -> None:
    with pytest.raises(VigilConfigurationInputError):
        validate_configuration_input(
            interval_days=30,
            grace_hours=72,
            recipients=[{"email": "not-an-email", "email_confirm": "not-an-email"}],
            message="",
        )


def test_validate_configuration_input_rejects_message_over_4000_codepoints() -> None:
    with pytest.raises(VigilConfigurationInputError):
        validate_configuration_input(
            interval_days=30,
            grace_hours=72,
            recipients=_recipients("a@example.com"),
            message="x" * 4001,
        )


def test_validate_configuration_input_accepts_message_at_4000_codepoints() -> None:
    validate_configuration_input(
        interval_days=30,
        grace_hours=72,
        recipients=_recipients("a@example.com"),
        message="x" * 4000,
    )
