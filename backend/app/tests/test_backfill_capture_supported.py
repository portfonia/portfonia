"""Tests for the issue #313 item 4 one-off backfill script
(`app/scripts/backfill_capture_supported.py`).

Migration c7d8e9f0a1b2 (issue #311) defaults every pre-existing row —
including ones already stored as market="Other" — to capture_supported=True,
because it runs raw SQL against encrypted ciphertext and cannot decrypt
`ticker` to re-resolve it. This script re-runs `resolve_holding_market`
(the same function `confirm_holdings` calls on every fresh upload) against
existing Other rows so they pick up the correct flag without a silent
migration-time rewrite.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.scripts.backfill_capture_supported import backfill_capture_supported
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def _holding(**kw: object) -> Holding:
    data: dict[str, object] = dict(
        user_id=_USER,
        pricing_mode="auto",
        currency="USD",
        shares=Decimal("10"),
        asset_class="STOCK",
        market="Other",
        capture_supported=True,
    )
    data.update(kw)
    return Holding(**data)


def test_reclassifies_now_resolvable_ticker_out_of_other(db_session: Session) -> None:
    """A UK holding uploaded before #311 shipped the UK node was stored as
    Other/capture_supported=True (the only value that existed pre-#311).
    Re-resolving now must move it to market="UK" — it was never actually
    unsupported, the capture buckets just didn't exist yet."""
    seed_user(db_session, _USER)
    h = _holding(name="Vodafone", ticker="VOD.L")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 1
    assert h.market == "UK"
    assert h.capture_supported is True


def test_flips_capture_supported_false_for_still_unresolvable_ticker(
    db_session: Session,
) -> None:
    """A genuinely unresolvable ticker must flip capture_supported to False
    so section 1 renders '[market not supported]' instead of sitting
    silently as capture_supported=True Other forever."""
    seed_user(db_session, _USER)
    h = _holding(name="Unresolvable Foreign Listing", ticker="XYZ.WEIRD")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 1
    assert h.market == "Other"
    assert h.capture_supported is False


def test_dry_run_reports_the_same_count_without_mutating(db_session: Session) -> None:
    seed_user(db_session, _USER)
    h = _holding(name="Vodafone", ticker="VOD.L")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=False)

    assert changed == 1
    assert h.market == "Other"  # untouched — dry run
    assert h.capture_supported is True


def test_idempotent_second_pass_is_a_noop(db_session: Session) -> None:
    seed_user(db_session, _USER)
    h = _holding(name="Vodafone", ticker="VOD.L")
    db_session.add(h)
    db_session.flush()

    backfill_capture_supported(db_session, apply_changes=True)
    second_pass = backfill_capture_supported(db_session, apply_changes=True)

    assert second_pass == 0


def test_never_touches_holdings_not_stored_as_other(db_session: Session) -> None:
    """Only market="Other" rows are in scope — a row that already resolved
    correctly at parse time must be left exactly as-is."""
    seed_user(db_session, _USER)
    h = _holding(name="NVIDIA", ticker="NVDA", market="US")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 0
    assert h.market == "US"
