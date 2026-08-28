"""Tests for app/services/accounts.py (issue #129 B7 review)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.services.accounts import resolve_accounts_for_holdings
from app.tests.conftest import seed_user

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000e1")


def test_new_broker_creates_an_account(db_session: Session) -> None:
    seed_user(db_session, _USER)
    ids = resolve_accounts_for_holdings(db_session, _USER, [("Schwab", None, None)])
    assert ids[0] is not None
    account = db_session.get(Account, ids[0])
    assert account is not None
    assert account.broker == "Schwab"
    assert account.user_id == _USER


def test_none_broker_yields_none_account_id(db_session: Session) -> None:
    seed_user(db_session, _USER)
    ids = resolve_accounts_for_holdings(db_session, _USER, [(None, None, None)])
    assert ids == [None]
    assert db_session.execute(select(Account).where(Account.user_id == _USER)).first() is None


def test_matching_tuple_reuses_the_same_account(db_session: Session) -> None:
    seed_user(db_session, _USER)
    ids = resolve_accounts_for_holdings(
        db_session,
        _USER,
        [("Futu", "Main", None), ("Futu", "Main", None)],
    )
    assert ids[0] == ids[1]
    accounts = list(db_session.execute(select(Account).where(Account.user_id == _USER)).scalars())
    assert len(accounts) == 1


def test_distinct_account_or_portfolio_stays_distinct(db_session: Session) -> None:
    seed_user(db_session, _USER)
    ids = resolve_accounts_for_holdings(
        db_session,
        _USER,
        [("IBKR", "Family", None), ("IBKR", "Personal", None), ("IBKR", "Family", "Growth")],
    )
    assert len({i for i in ids if i is not None}) == 3


def test_call_reuses_existing_account_from_a_prior_call(db_session: Session) -> None:
    """Simulates two confirms: the second must not create a duplicate for
    the same (broker, account, portfolio)."""
    seed_user(db_session, _USER)
    first = resolve_accounts_for_holdings(db_session, _USER, [("Schwab", None, None)])
    db_session.flush()
    second = resolve_accounts_for_holdings(db_session, _USER, [("Schwab", None, None)])
    assert first == second
    accounts = list(db_session.execute(select(Account).where(Account.user_id == _USER)).scalars())
    assert len(accounts) == 1


def test_account_no_longer_referenced_is_archived_not_deleted(db_session: Session) -> None:
    seed_user(db_session, _USER)
    ids = resolve_accounts_for_holdings(db_session, _USER, [("Schwab", None, None)])
    db_session.flush()
    old_account_id = ids[0]
    assert old_account_id is not None

    # Next call replaces Schwab with IBKR entirely.
    resolve_accounts_for_holdings(db_session, _USER, [("IBKR", None, None)])
    db_session.flush()

    old_account = db_session.get(Account, old_account_id)
    assert old_account is not None  # not deleted
    assert old_account.archived_at is not None


def test_archived_account_is_not_reused(db_session: Session) -> None:
    """An archived account for the same (broker, account, portfolio) must
    not be silently revived — a fresh account is created instead, so an
    archived account's history stays a closed chapter."""
    seed_user(db_session, _USER)
    first_ids = resolve_accounts_for_holdings(db_session, _USER, [("Schwab", None, None)])
    db_session.flush()
    resolve_accounts_for_holdings(db_session, _USER, [("IBKR", None, None)])  # archives Schwab
    db_session.flush()

    second_ids = resolve_accounts_for_holdings(db_session, _USER, [("Schwab", None, None)])
    assert second_ids[0] != first_ids[0]


def test_blank_broker_yields_none_account_id(db_session: Session) -> None:
    """review, PR #247 round 2: report_sections.py/_summarize both already
    treat "" and whitespace-only broker as equivalent to no broker — the
    resolver must match, not silently create a real accounts row for it."""
    seed_user(db_session, _USER)
    ids = resolve_accounts_for_holdings(db_session, _USER, [("", None, None), ("   ", None, None)])
    assert ids == [None, None]
    assert db_session.execute(select(Account).where(Account.user_id == _USER)).first() is None


def test_padded_broker_matches_the_unpadded_form(db_session: Session) -> None:
    """ " IBKR " and "IBKR" must resolve to the SAME account — otherwise one
    custodian silently fans out into several, the opposite of the
    migration's grouping intent."""
    seed_user(db_session, _USER)
    ids = resolve_accounts_for_holdings(
        db_session, _USER, [(" IBKR ", None, None), ("IBKR", None, None)]
    )
    assert ids[0] == ids[1]
    account = db_session.get(Account, ids[0])
    assert account is not None
    assert account.broker == "IBKR"


def test_blank_account_and_portfolio_are_treated_as_none(db_session: Session) -> None:
    seed_user(db_session, _USER)
    ids = resolve_accounts_for_holdings(
        db_session, _USER, [("Schwab", "", "  "), ("Schwab", None, None)]
    )
    assert ids[0] == ids[1]


def test_two_users_never_share_an_account_even_with_identical_broker(
    db_session: Session,
) -> None:
    other_user = uuid.uuid4()
    seed_user(db_session, _USER)
    seed_user(db_session, other_user)
    mine = resolve_accounts_for_holdings(db_session, _USER, [("Schwab", None, None)])
    theirs = resolve_accounts_for_holdings(db_session, other_user, [("Schwab", None, None)])
    assert mine[0] != theirs[0]
