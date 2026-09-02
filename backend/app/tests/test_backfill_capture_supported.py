"""Tests for the issue #313 item 4 one-off backfill script
(`app/scripts/backfill_capture_supported.py`).

Migration c7d8e9f0a1b2 (issue #311) defaults every pre-existing row —
including ones already stored as market="Other" — to capture_supported=True,
because it runs raw SQL against encrypted ciphertext and cannot decrypt
`ticker` to re-resolve it. This script re-runs the same confirm-parity
sequence `_apply_write_defaults` (routers/holdings.py) already applies to
every fresh write: normalize -> apply_confirmed_exchange_suffix -> normalize
-> resolve_holding_market, against existing Other rows.

PR #314 review round 1 caught a real bug in an earlier version of this
script: calling `resolve_holding_market` directly (skipping the
suffix-forcing step) silently promoted every still-bare, currency-hinted
Other ticker (e.g. "VOD" + GBP) to market="US"/capture_supported=True
instead of UK/"VOD.L" — the #204 collision class this capture series exists
to kill. `test_reclassifies_bare_ticker_with_currency_hint_not_to_us` below
is the regression test for that exact bug.
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


def test_reclassifies_already_suffixed_ticker_out_of_other(db_session: Session) -> None:
    """A UK holding uploaded before #311 shipped the UK node, already stored
    with its real LSE suffix, was stuck as Other/capture_supported=True (the
    only value that existed pre-#311). Re-resolving must move it to
    market="UK" — it was never actually unsupported, the capture buckets
    just didn't exist yet."""
    seed_user(db_session, _USER)
    h = _holding(name="Vodafone", ticker="VOD.L", currency="GBP")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 1
    assert h.market == "UK"
    assert h.ticker == "VOD.L"
    assert h.capture_supported is True


def test_reclassifies_bare_ticker_with_currency_hint_not_to_us(db_session: Session) -> None:
    """issue #313 PR #314 review round 1: a still-bare ticker ("VOD", no
    exchange suffix) with a currency hint (GBP) must get the suffix forced
    BEFORE market resolution runs — same as apply_confirmed_exchange_suffix
    does for every fresh write — so it reclassifies as UK/"VOD.L", never as
    a bare-ticker "no suffix = US" promotion (VOD also happens to be a real
    NYSE-listed ADR ticker, so silently promoting to US would price the
    wrong instrument against GBP-denominated LSE holding data)."""
    seed_user(db_session, _USER)
    h = _holding(name="Vodafone", ticker="VOD", currency="GBP")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 1
    assert h.market == "UK"
    assert h.ticker == "VOD.L"
    assert h.capture_supported is True


def test_bare_ticker_with_ambiguous_multi_venue_currency_stays_capture_unsupported(
    db_session: Session,
) -> None:
    """A bare ticker with a EUR currency hint resolves a market (Europe) but
    not a specific venue (.AS/.PA/.DE are all plausible) — matches
    apply_confirmed_exchange_suffix's `_suffix_ambiguous` gate: the market is
    still persisted (better than Other) but capture_supported stays False,
    never silently promoted to a guessed venue."""
    seed_user(db_session, _USER)
    h = _holding(name="Some European Listing", ticker="XYZ", currency="EUR")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 1
    assert h.market == "Europe"
    assert h.ticker == "XYZ"
    assert h.capture_supported is False


def test_bare_ticker_with_usd_currency_promotes_to_us_consistent_with_write_path(
    db_session: Session,
) -> None:
    """A bare ticker with a USD hint promoting to market="US" is not a bug —
    it's the same outcome _apply_write_defaults produces for a fresh USD
    holding with no exchange suffix, and USD carries no venue ambiguity."""
    seed_user(db_session, _USER)
    h = _holding(name="Some US Stock", ticker="ZZZQ", currency="USD")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 1
    assert h.market == "US"
    assert h.ticker == "ZZZQ"
    assert h.capture_supported is True


def test_flips_capture_supported_false_for_still_unresolvable_ticker(
    db_session: Session,
) -> None:
    """A ticker with an unrecognized suffix (not a bare ticker, not a known
    exchange suffix) must flip capture_supported to False so section 1
    renders '[market not supported]' instead of sitting silently as
    capture_supported=True Other forever."""
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
    h = _holding(name="Vodafone", ticker="VOD.L", currency="GBP")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=False)

    assert changed == 1
    assert h.market == "Other"  # untouched — dry run
    assert h.capture_supported is True


def test_idempotent_second_pass_is_a_noop(db_session: Session) -> None:
    seed_user(db_session, _USER)
    h = _holding(name="Vodafone", ticker="VOD", currency="GBP")
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


def test_cash_row_with_no_ticker_is_unchanged(db_session: Session) -> None:
    """The normal issue-#120 shape: a cash/wmf row stored as Other with no
    ticker at all. `resolve_holding_market` already returns ("Other", True)
    for it regardless of the seeded-None declared_market, so this is a
    no-op — but PR #314 review round 2 asked for explicit coverage rather
    than relying on that being true by accident of the resolution order."""
    seed_user(db_session, _USER)
    h = _holding(name="Cash", ticker=None, asset_type="cash", currency="USD")
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 0
    assert h.market == "Other"
    assert h.capture_supported is True


def test_legacy_cash_row_with_spurious_ticker_is_not_promoted(db_session: Session) -> None:
    """PR #314 review round 2: a cash/wmf row that predates issue #120 can
    still carry a spurious ticker string (e.g. "CASH") instead of no ticker
    at all. Without the asset_type skip, `_resolve` would hit
    `market_from_ticker`'s bare-ticker branch before ever reaching
    `resolve_holding_market`'s asset_type check, promoting the row to
    market="US"/capture_supported=True — the wrong stored market, even
    though capture still skips it via pricing_mode="manual" so no wrong
    price actually gets fetched. The backfill must leave asset_type
    "cash"/"wmf" rows alone regardless of what ticker they carry."""
    seed_user(db_session, _USER)
    h = _holding(
        name="Cash",
        ticker="CASH",
        asset_type="cash",
        pricing_mode="manual",
        currency="USD",
    )
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 0
    assert h.market == "Other"
    assert h.ticker == "CASH"
    assert h.capture_supported is True


def test_legacy_wmf_row_with_spurious_ticker_is_not_promoted(db_session: Session) -> None:
    """Same corner case as the cash test above, for the sibling asset_type
    "wmf" (wealth management product) — the backfill's skip must cover both
    values named in issue #313 item 1, not just "cash"."""
    seed_user(db_session, _USER)
    h = _holding(
        name="Bank WMP",
        ticker="WMP001",
        asset_type="wmf",
        pricing_mode="manual",
        currency="CNY",
    )
    db_session.add(h)
    db_session.flush()

    changed = backfill_capture_supported(db_session, apply_changes=True)

    assert changed == 0
    assert h.market == "Other"
    assert h.ticker == "WMP001"
    assert h.capture_supported is True
