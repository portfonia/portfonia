"""DELETE /admin/users/{user_id} hard purge (issue #199; Supabase Auth
purge + orphan-only path, issue #225)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.account import Account
from app.models.email_verification import EmailVerification
from app.models.holding import Holding
from app.models.invite import Invite
from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.models.price_snapshot import PriceSnapshot
from app.models.report import Report
from app.models.upload_job import UploadJob
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.services.auth_provider import AuthProviderError, AuthUserInfo
from app.services.invites import hash_invite_token
from app.services.questionnaire_taxonomy import QUESTIONNAIRE_VERSION
from app.tests.test_admin_router import _headers
from app.tests.test_user_scope import _h, _user

_A = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_B = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
_UNKNOWN = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
# A distinct value from any `users.id` above, used as an `auth_subject` —
# `_user()`'s default `f"sub-{user_id}"` embeds the row's own id, which
# would make a PK lookup on that value a hit, not the miss the round-2
# regression test needs.
_AUTH_SUB = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

_VALID_QUESTIONNAIRE: dict[str, object] = {
    "asset_scale": "100K_500K",
    "markets": ["US", "HK"],
    "style": "GROWTH",
    "horizon": "LONG",
    "risk_appetite": "BALANCED",
    "sectors_of_interest": ["Technology", "Healthcare"],
    "objective": "GROWTH",
    "intel_focus": "MACRO",
}


def _path(user_id: uuid.UUID) -> str:
    return f"/admin/users/{user_id}"


def _count(session: Session, column: Any, user_id: uuid.UUID) -> int:
    return int(session.execute(select(func.count()).where(column == user_id)).scalar_one())


def _context(user_id: uuid.UUID) -> UserInvestmentContext:
    return UserInvestmentContext(
        user_id=user_id,
        questionnaire=_VALID_QUESTIONNAIRE,
        questionnaire_version=QUESTIONNAIRE_VERSION,
    )


def _report(user_id: uuid.UUID, *, session_node: str = "manual") -> Report:
    return Report(
        user_id=user_id,
        report_date=date(2026, 8, 26),
        report_type="incremental",
        session_node=session_node,
        status="success",
        report_md="purge-test",
    )


def _job(user_id: uuid.UUID) -> UploadJob:
    return UploadJob(user_id=user_id, filename="book.csv", status="success")


@pytest.fixture(autouse=True)
def _fake_delete_auth_user(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Existing purge tests predate issue #225 and don't care about Auth
    deletion — default it to a no-op success so `_user()`'s always-set
    `auth_subject` doesn't make every one of them hit real Supabase.
    Tests that care about this call override with their own monkeypatch."""
    mock = MagicMock(return_value=True)
    monkeypatch.setattr("app.routers.admin.delete_auth_user", mock)
    return mock


@pytest.fixture(autouse=True)
def _fake_get_auth_user(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Default the orphan-path lookup to "nothing there" (review, PR #246
    round 1: without this, `test_purge_unknown_uuid_404` — unchanged by
    issue #225 — falls into the new no-local-row branch and calls the real
    `get_auth_user` against whatever Settings loads from .env.local, a live
    admin API call the repo's test convention forbids). Tests that exercise
    the orphan-found path override this with their own monkeypatch."""
    mock = MagicMock(return_value=None)
    monkeypatch.setattr("app.routers.admin.get_auth_user", mock)
    return mock


def test_purge_requires_ops_token(app_client: TestClient) -> None:
    resp = app_client.delete(_path(_A), params={"confirm": "a@example.com"})
    assert resp.status_code == 401


def test_purge_unknown_uuid_404(app_client: TestClient) -> None:
    resp = app_client.delete(
        _path(_UNKNOWN), headers=_headers(), params={"confirm": "nobody@example.com"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "user not found"


def test_purge_missing_confirm_422(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    resp = app_client.delete(_path(_A), headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["detail"] == "confirm query param is required"


def test_purge_confirm_wrong_email_409(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "other@example.com"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "confirm does not match user email"
    assert db_session.get(User, _A) is not None


def test_purge_confirm_case_and_whitespace_succeeds(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_A, "foo@bar.com"))
    db_session.flush()
    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": " Foo@Bar.com "})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(_A)
    assert body["email"] == "foo@bar.com"
    assert body["deleted"]["users"] == 1
    assert db_session.get(User, _A) is None


def test_purge_seed_user_409(app_client: TestClient, db_session: Session) -> None:
    seed_id = uuid.UUID(get_settings().DEV_USER_ID)
    db_session.add(_user(seed_id, "seed@example.com"))
    db_session.flush()
    resp = app_client.delete(
        _path(seed_id), headers=_headers(), params={"confirm": "seed@example.com"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "refusing to delete the seed user"
    assert db_session.get(User, seed_id) is not None


def test_purge_refuses_user_who_created_invites(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(
        Invite(
            token_hash=hash_invite_token("created-by-a"),
            created_by=_A,
            expires_at=datetime.now(tz=UTC) + timedelta(days=14),
        )
    )
    db_session.flush()
    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "user created invites; revoke or reassign first"
    assert db_session.get(User, _A) is not None


def test_purge_happy_path_two_users(app_client: TestClient, db_session: Session) -> None:
    news = News(
        url_hash="purge-news-hash",
        title="Fed holds rates",
        source="Reuters",
        url="https://example.com/fed",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    snap = PriceSnapshot(
        ticker="NVDA",
        market="US",
        session_node="close",
        trade_date=date(2026, 8, 1),
        close=Decimal("120.0"),
    )
    user_a = _user(_A, "a@example.com")
    user_b = _user(_B, "b@example.com")
    user_b.invited_by = _A
    report_a = _report(_A, session_node="manual")
    report_b = _report(_B, session_node="manual")
    used_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    redeemed = Invite(
        token_hash=hash_invite_token("redeemed-by-a"),
        created_by=_B,
        expires_at=datetime.now(tz=UTC) + timedelta(days=14),
        used_at=used_at,
        used_by_user_id=_A,
    )
    db_session.add_all(
        [
            user_a,
            user_b,
            news,
            snap,
            report_a,
            report_b,
            _h(user_id=_A, name="NVIDIA", ticker="NVDA"),
            _h(user_id=_A, name="Apple", ticker="AAPL"),
            _h(user_id=_B, name="Tencent", ticker="0700.HK", currency="HKD"),
            _job(_A),
            _job(_B),
            _context(_A),
            _context(_B),
            redeemed,
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            NewsSurfaced(user_id=_A, news_id=news.id, report_id=report_a.id),
            NewsSurfaced(user_id=_B, news_id=news.id, report_id=report_b.id),
        ]
    )
    db_session.flush()
    invite_id = redeemed.id
    news_id = news.id
    snap_id = snap.id

    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(_A)
    assert body["email"] == "a@example.com"
    assert body["deleted"] == {
        "news_surfaced": 1,
        "reports": 1,
        "holdings": 2,
        "accounts": 0,
        "upload_jobs": 1,
        "user_investment_context": 1,
        "email_verifications": 0,
        "invites_used_by_cleared": 1,
        "users_invited_by_cleared": 1,
        "users": 1,
    }

    db_session.expire_all()
    assert db_session.get(User, _A) is None
    assert _count(db_session, Holding.user_id, _A) == 0
    assert _count(db_session, Report.user_id, _A) == 0
    assert _count(db_session, UploadJob.user_id, _A) == 0
    assert _count(db_session, NewsSurfaced.user_id, _A) == 0
    assert db_session.get(UserInvestmentContext, _A) is None

    b = db_session.get(User, _B)
    assert b is not None
    assert b.invited_by is None
    assert _count(db_session, Holding.user_id, _B) == 1
    assert _count(db_session, Report.user_id, _B) == 1
    assert _count(db_session, UploadJob.user_id, _B) == 1
    assert _count(db_session, NewsSurfaced.user_id, _B) == 1
    assert db_session.get(UserInvestmentContext, _B) is not None

    assert db_session.get(News, news_id) is not None
    assert db_session.get(PriceSnapshot, snap_id) is not None

    invite = db_session.get(Invite, invite_id)
    assert invite is not None
    assert invite.used_at is not None
    assert invite.used_by_user_id is None

    second = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})
    assert second.status_code == 404
    assert second.json()["detail"] == "user not found"


def test_purge_without_investment_context_counts_zero(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})
    assert resp.status_code == 200
    assert resp.json()["deleted"]["user_investment_context"] == 0
    assert resp.json()["deleted"]["users"] == 1


def test_deleting_users_while_context_exists_hits_fk(db_session: Session) -> None:
    """Real FK: user_investment_context.user_id -> users.id. A purge that
    deletes the users row first fails here; purge_user must delete context
    while the users row still exists.
    """
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(_context(_A))
    db_session.flush()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(User).where(User.id == _A))
        db_session.flush()


def test_purge_user_deletes_context_while_users_row_exists(db_session: Session) -> None:
    from app.services.user_purge import purge_user

    db_session.add(_user(_A, "a@example.com"))
    db_session.add(_context(_A))
    db_session.flush()
    result = purge_user(db_session, _A)
    db_session.commit()
    assert result.user_investment_context == 1
    assert result.users == 1
    db_session.expire_all()
    assert db_session.get(User, _A) is None
    assert db_session.get(UserInvestmentContext, _A) is None


# --- issue #225: Auth deletion sequencing + orphan-only purge path -----


def test_purge_with_auth_subject_deletes_supabase_user(
    app_client: TestClient, db_session: Session, _fake_delete_auth_user: MagicMock
) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})
    assert resp.status_code == 200
    assert resp.json()["auth_deleted"] is True
    _fake_delete_auth_user.assert_called_once_with(f"sub-{_A}")


def test_purge_without_auth_subject_leaves_auth_deleted_false(
    app_client: TestClient, db_session: Session, _fake_delete_auth_user: MagicMock
) -> None:
    user = _user(_A, "a@example.com")
    user.auth_subject = None
    db_session.add(user)
    db_session.flush()
    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})
    assert resp.status_code == 200
    assert resp.json()["auth_deleted"] is False
    _fake_delete_auth_user.assert_not_called()


def test_purge_auth_provider_error_502_touches_no_local_rows(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard (issue #225 acceptance criteria): a failure deleting
    the Supabase Auth user must leave every local row untouched — the
    request is a clean no-op, safely retryable, never a half purge."""
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(_h(user_id=_A, name="NVIDIA", ticker="NVDA"))
    db_session.flush()
    monkeypatch.setattr(
        "app.routers.admin.delete_auth_user",
        MagicMock(side_effect=AuthProviderError("boom")),
    )
    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})
    assert resp.status_code == 502
    db_session.expire_all()
    assert db_session.get(User, _A) is not None
    assert _count(db_session, Holding.user_id, _A) == 1


def test_purge_orphan_auth_user_found(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement B: no local row, but a Supabase Auth account remains —
    the exact gap issue #225 was opened to close."""
    delete_mock = MagicMock(return_value=True)
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user",
        MagicMock(return_value=AuthUserInfo(id=str(_UNKNOWN), email="orphan@example.com")),
    )
    monkeypatch.setattr("app.routers.admin.delete_auth_user", delete_mock)
    resp = app_client.delete(
        _path(_UNKNOWN), headers=_headers(), params={"confirm": "orphan@example.com"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_deleted"] is True
    assert body["email"] == "orphan@example.com"
    assert body["deleted"] == {
        "news_surfaced": 0,
        "reports": 0,
        "holdings": 0,
        "accounts": 0,
        "upload_jobs": 0,
        "user_investment_context": 0,
        "email_verifications": 0,
        "invites_used_by_cleared": 0,
        "users_invited_by_cleared": 0,
        "users": 0,
    }
    delete_mock.assert_called_once_with(str(_UNKNOWN))


def test_purge_orphan_auth_user_not_found_404(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither side has anything — the only case that still 404s."""
    monkeypatch.setattr("app.routers.admin.get_auth_user", MagicMock(return_value=None))
    resp = app_client.delete(
        _path(_UNKNOWN), headers=_headers(), params={"confirm": "nobody@example.com"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "user not found"


def test_purge_orphan_auth_user_missing_confirm_422(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user",
        MagicMock(return_value=AuthUserInfo(id=str(_UNKNOWN), email="orphan@example.com")),
    )
    resp = app_client.delete(_path(_UNKNOWN), headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["detail"] == "confirm query param is required"


def test_purge_orphan_auth_user_confirm_mismatch_409(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    delete_mock = MagicMock(return_value=True)
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user",
        MagicMock(return_value=AuthUserInfo(id=str(_UNKNOWN), email="orphan@example.com")),
    )
    monkeypatch.setattr("app.routers.admin.delete_auth_user", delete_mock)
    resp = app_client.delete(
        _path(_UNKNOWN), headers=_headers(), params={"confirm": "someone-else@example.com"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "confirm does not match user email"
    delete_mock.assert_not_called()


def test_purge_orphan_auth_user_delete_failure_502(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user",
        MagicMock(return_value=AuthUserInfo(id=str(_UNKNOWN), email="orphan@example.com")),
    )
    monkeypatch.setattr(
        "app.routers.admin.delete_auth_user",
        MagicMock(side_effect=AuthProviderError("boom")),
    )
    resp = app_client.delete(
        _path(_UNKNOWN), headers=_headers(), params={"confirm": "orphan@example.com"}
    )
    assert resp.status_code == 502


def test_purge_orphan_auth_user_lookup_failure_502(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #246 round 1 review: the GET half of the orphan path had no
    AuthProviderError mapping at all, so a GoTrue 5xx/timeout surfaced as an
    unhandled 500 instead of the documented, retry-safe 502."""
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user",
        MagicMock(side_effect=AuthProviderError("boom")),
    )
    resp = app_client.delete(
        _path(_UNKNOWN), headers=_headers(), params={"confirm": "orphan@example.com"}
    )
    assert resp.status_code == 502


def test_purge_by_auth_subject_of_live_user_refused_409(
    app_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    _fake_get_auth_user: MagicMock,
) -> None:
    """PR #246 round 2 review: a PK miss on `users.id` is not proof there's
    no local user — `user_id` could be a live user's `auth_subject` passed
    by mistake (both are UUIDs, easy to confuse). Falling through to the
    orphan path would Auth-delete a live account while its local row (a
    different id) sits untouched. Must 409 before ever calling Auth."""
    user = _user(_A, "a@example.com")
    user.auth_subject = str(_AUTH_SUB)
    db_session.add(user)
    db_session.flush()

    resp = app_client.delete(
        _path(_AUTH_SUB), headers=_headers(), params={"confirm": "a@example.com"}
    )
    assert resp.status_code == 409
    assert str(_A) in resp.json()["detail"]
    _fake_get_auth_user.assert_not_called()
    db_session.expire_all()
    assert db_session.get(User, _A) is not None


def test_purge_by_seed_users_auth_subject_refused_409(
    app_client: TestClient,
    db_session: Session,
    _fake_get_auth_user: MagicMock,
) -> None:
    """Same guard, seed user specifically: the existing `refusing to delete
    the seed user` 409 only fires when `{user_id}` is the seed's own PK.
    Calling with the seed's `auth_subject` instead must not slip past it
    into a live Auth deletion."""
    seed_id = uuid.UUID(get_settings().DEV_USER_ID)
    seed = _user(seed_id, "seed@example.com")
    seed.auth_subject = str(_AUTH_SUB)
    db_session.add(seed)
    db_session.flush()

    resp = app_client.delete(
        _path(_AUTH_SUB), headers=_headers(), params={"confirm": "seed@example.com"}
    )
    assert resp.status_code == 409
    _fake_get_auth_user.assert_not_called()
    db_session.expire_all()
    assert db_session.get(User, seed_id) is not None


# --- issue #129 checkpoint B7: accounts table + user_id FKs -------------


def test_purge_deletes_accounts_and_clears_holdings_account_id(
    app_client: TestClient, db_session: Session
) -> None:
    """B7: `accounts` rows are this user's own data too — purge must clean
    them up, and must do so without tripping holdings.account_id's FK
    (accounts is deleted after holdings, never before)."""
    db_session.add(_user(_A, "a@example.com"))
    acct = Account(user_id=_A, broker="Schwab")
    db_session.add(acct)
    db_session.flush()
    db_session.add(
        _h(user_id=_A, name="NVIDIA", ticker="NVDA", broker="Schwab", account_id=acct.id)
    )
    db_session.flush()

    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"]["accounts"] == 1
    db_session.expire_all()
    assert _count(db_session, Account.user_id, _A) == 0


def test_purge_does_not_touch_another_users_accounts(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add_all([_user(_A, "a@example.com"), _user(_B, "b@example.com")])
    db_session.flush()
    other_acct = Account(user_id=_B, broker="IBKR")
    db_session.add(other_acct)
    db_session.flush()
    other_acct_id = other_acct.id

    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Account, other_acct_id) is not None


def test_deleting_holdings_user_id_out_of_order_hits_fk(db_session: Session) -> None:
    """Real FK: holdings.user_id -> users.id (B7). A bare DELETE FROM users
    with a holding still pointing at it must fail — this is the acceptance
    guard design §9.4 requires: forgetting a step in a hand-rolled delete
    must surface as an explicit FK error, not silently orphaned data."""
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(_h(user_id=_A, name="NVIDIA", ticker="NVDA"))
    db_session.flush()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(User).where(User.id == _A))
        db_session.flush()


def test_deleting_reports_user_id_out_of_order_hits_fk(db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(_report(_A))
    db_session.flush()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(User).where(User.id == _A))
        db_session.flush()


def test_deleting_upload_jobs_user_id_out_of_order_hits_fk(db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(_job(_A))
    db_session.flush()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(User).where(User.id == _A))
        db_session.flush()


def test_deleting_news_surfaced_user_id_out_of_order_hits_fk(db_session: Session) -> None:
    news = News(
        url_hash="b7-fk-news-hash",
        title="Fed holds rates",
        source="Reuters",
        url="https://example.com/fed-b7",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    user = _user(_A, "a@example.com")
    report = _report(_A)
    db_session.add_all([user, news, report])
    db_session.flush()
    db_session.add(NewsSurfaced(user_id=_A, news_id=news.id, report_id=report.id))
    db_session.flush()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(User).where(User.id == _A))
        db_session.flush()


def test_deleting_accounts_before_holdings_hits_fk(db_session: Session) -> None:
    """Real FK: holdings.account_id -> accounts.id (B7). Confirms the
    ordering purge_user() relies on (holdings before accounts) is load-
    bearing, not just convention."""
    db_session.add(_user(_A, "a@example.com"))
    acct = Account(user_id=_A, broker="Schwab")
    db_session.add(acct)
    db_session.flush()
    db_session.add(_h(user_id=_A, name="NVIDIA", ticker="NVDA", account_id=acct.id))
    db_session.flush()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(Account).where(Account.id == acct.id))
        db_session.flush()


def test_holding_cannot_point_at_another_users_account(db_session: Session) -> None:
    """Real composite FK: holdings (account_id, user_id) -> accounts (id,
    user_id) (B7 review round 2) — a single-column FK on account_id alone
    would only guarantee the account exists, not that it's this holding's
    own user's account. A holding under user B pointing at user A's
    account must fail at flush, not silently succeed."""
    db_session.add_all([_user(_A, "a@example.com"), _user(_B, "b@example.com")])
    acct_a = Account(user_id=_A, broker="Schwab")
    db_session.add(acct_a)
    db_session.flush()
    db_session.add(_h(user_id=_B, name="NVIDIA", ticker="NVDA", account_id=acct_a.id))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_holding_with_null_account_id_and_a_real_user_id_is_unaffected(
    db_session: Session,
) -> None:
    """MATCH SIMPLE (Postgres default) skips the composite FK check
    entirely when any column is NULL — a holding with no account must not
    be rejected just because user_id is non-NULL."""
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(_h(user_id=_A, name="NVIDIA", ticker="NVDA", account_id=None))
    db_session.flush()  # must not raise


# --- issue #260: email_verifications table + user_id FK ------------------


def _verification(
    user_id: uuid.UUID | None, *, token_hash: str, purpose: str = "delivery_email"
) -> EmailVerification:
    now = datetime.now(tz=UTC)
    return EmailVerification(
        user_id=user_id,
        purpose=purpose,
        email="candidate@example.com",
        token_hash=token_hash,
        status="pending",
        expires_at=now + timedelta(hours=48),
        last_sent_at=now,
    )


def test_purge_deletes_bound_email_verifications(
    app_client: TestClient, db_session: Session
) -> None:
    """Review, PR #261: email_verifications.user_id FKs to users.id ON
    DELETE RESTRICT (issue #260) — before this fix, purging a user with a
    bound (account_email/delivery_email) verification row raised
    IntegrityError instead of purging, the same class of break B7 already
    paid for on holdings/reports/upload_jobs/news_surfaced."""
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(_verification(_A, token_hash="bound-token-a"))
    db_session.flush()

    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"]["email_verifications"] == 1
    db_session.expire_all()
    assert _count(db_session, EmailVerification.user_id, _A) == 0


def test_purge_does_not_touch_another_users_email_verifications(
    app_client: TestClient, db_session: Session
) -> None:
    db_session.add_all([_user(_A, "a@example.com"), _user(_B, "b@example.com")])
    db_session.flush()
    other = _verification(_B, token_hash="bound-token-b")
    db_session.add(other)
    db_session.flush()
    other_id = other.id

    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})

    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(EmailVerification, other_id) is not None


def test_purge_does_not_touch_unbound_ops_manual_verifications(
    app_client: TestClient, db_session: Session
) -> None:
    """An ops_manual probe (user_id NULL) belongs to no account — purging a
    real user must not touch it, and the FK (nullable) never blocks it."""
    db_session.add(_user(_A, "a@example.com"))
    unbound = _verification(None, token_hash="unbound-token", purpose="ops_manual")
    db_session.add(unbound)
    db_session.flush()
    unbound_id = unbound.id

    resp = app_client.delete(_path(_A), headers=_headers(), params={"confirm": "a@example.com"})

    assert resp.status_code == 200
    assert resp.json()["deleted"]["email_verifications"] == 0
    db_session.expire_all()
    assert db_session.get(EmailVerification, unbound_id) is not None


# --- issue #274: DELETE /admin/users/by-email --------------------------------


def _by_email_path() -> str:
    return "/admin/users/by-email"


@pytest.fixture(autouse=True)
def _fake_get_auth_user_by_email(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Same default as _fake_get_auth_user above: a local miss on the
    by-email route must not fall through to a real Supabase admin call.
    Tests exercising the orphan-found path override with their own mock.
    raising=False so the patch is a no-op if the symbol isn't there yet
    (matches the conftest _no_external_notifications convention)."""
    mock = MagicMock(return_value=None)
    monkeypatch.setattr("app.routers.admin.get_auth_user_by_email", mock, raising=False)
    return mock


def test_purge_by_email_requires_ops_token(app_client: TestClient) -> None:
    """Also pins route ordering: if /users/by-email were captured by
    /users/{user_id}, the segment would fail UUID parsing and 422 before
    any auth check — a 401 proves the by-email route is matched first."""
    resp = app_client.delete(
        _by_email_path(),
        params={"email": "a@example.com", "confirm": "a@example.com"},
    )
    assert resp.status_code == 401


def test_purge_by_email_missing_params_422(
    app_client: TestClient, db_session: Session, _fake_get_auth_user_by_email: MagicMock
) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    resp = app_client.delete(_by_email_path(), headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["detail"] == "email and confirm query params are required"
    resp = app_client.delete(
        _by_email_path(), headers=_headers(), params={"email": "a@example.com"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "email and confirm query params are required"
    _fake_get_auth_user_by_email.assert_not_called()
    assert db_session.get(User, _A) is not None


def test_purge_by_email_confirm_mismatch_422(
    app_client: TestClient,
    db_session: Session,
    _fake_delete_auth_user: MagicMock,
    _fake_get_auth_user_by_email: MagicMock,
) -> None:
    """Boundary guard: the two values must be the same email — this is a
    repeat check, weaker than the by-id id/email cross-check by design
    (issue #274), and it fires before any lookup or delete."""
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "a@example.com", "confirm": "other@example.com"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "email and confirm must match"
    _fake_delete_auth_user.assert_not_called()
    _fake_get_auth_user_by_email.assert_not_called()
    assert db_session.get(User, _A) is not None


def test_purge_by_email_blank_params_422(
    app_client: TestClient, db_session: Session, _fake_get_auth_user_by_email: MagicMock
) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.flush()
    resp = app_client.delete(
        _by_email_path(), headers=_headers(), params={"email": "  ", "confirm": "  "}
    )
    assert resp.status_code == 422
    _fake_get_auth_user_by_email.assert_not_called()
    assert db_session.get(User, _A) is not None


def test_purge_by_email_case_and_whitespace_succeeds(
    app_client: TestClient, db_session: Session
) -> None:
    """Both params normalize (strip+lowercase) before comparison and before
    the local lookup — same _normalize_email discipline as signup."""
    db_session.add(_user(_A, "foo@bar.com"))
    db_session.flush()
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": " Foo@Bar.com ", "confirm": "foo@bar.com"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(_A)
    assert body["email"] == "foo@bar.com"
    assert body["deleted"]["users"] == 1
    assert db_session.get(User, _A) is None


def test_purge_by_email_local_hit_full_purge(
    app_client: TestClient, db_session: Session, _fake_delete_auth_user: MagicMock
) -> None:
    """Same 10-step ordered purge as the by-id route: holdings/reports/
    context/accounts/email_verifications plus the invite-pointer cleanup
    all run, the Supabase Auth account is deleted, and an unrelated user
    is untouched. Second call on the same email 404s."""
    db_session.add_all([_user(_A, "a@example.com"), _user(_B, "b@example.com")])
    db_session.add(_context(_A))
    db_session.add(_report(_A))
    db_session.add(_job(_A))
    acct = Account(user_id=_A, broker="Schwab")
    db_session.add(acct)
    db_session.flush()
    db_session.add(_h(user_id=_A, name="NVIDIA", ticker="NVDA", account_id=acct.id))
    db_session.add(_verification(_A, token_hash="by-email-token"))
    db_session.flush()
    redeemed = Invite(
        token_hash=hash_invite_token("redeemed-by-a"),
        created_by=_B,
        expires_at=datetime.now(tz=UTC) + timedelta(days=14),
        used_at=datetime.now(tz=UTC),
        used_by_user_id=_A,
    )
    db_session.add(redeemed)
    db_session.flush()
    invite_id = redeemed.id

    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "a@example.com", "confirm": "a@example.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(_A)
    assert body["email"] == "a@example.com"
    assert body["auth_deleted"] is True
    assert body["deleted"] == {
        "news_surfaced": 0,
        "reports": 1,
        "holdings": 1,
        "accounts": 1,
        "upload_jobs": 1,
        "user_investment_context": 1,
        "email_verifications": 1,
        "invites_used_by_cleared": 1,
        "users_invited_by_cleared": 0,
        "users": 1,
    }
    _fake_delete_auth_user.assert_called_once_with(f"sub-{_A}")

    db_session.expire_all()
    assert db_session.get(User, _A) is None
    assert db_session.get(User, _B) is not None
    invite = db_session.get(Invite, invite_id)
    assert invite is not None
    assert invite.used_by_user_id is None

    second = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "a@example.com", "confirm": "a@example.com"},
    )
    assert second.status_code == 404
    assert second.json()["detail"] == "user not found"


def test_purge_by_email_seed_user_409(app_client: TestClient, db_session: Session) -> None:
    seed_id = uuid.UUID(get_settings().DEV_USER_ID)
    db_session.add(_user(seed_id, "seed@example.com"))
    db_session.flush()
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "seed@example.com", "confirm": "seed@example.com"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "refusing to delete the seed user"
    assert db_session.get(User, seed_id) is not None


def test_purge_by_email_created_invites_409(app_client: TestClient, db_session: Session) -> None:
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(
        Invite(
            token_hash=hash_invite_token("created-by-a"),
            created_by=_A,
            expires_at=datetime.now(tz=UTC) + timedelta(days=14),
        )
    )
    db_session.flush()
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "a@example.com", "confirm": "a@example.com"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "user created invites; revoke or reassign first"
    assert db_session.get(User, _A) is not None


def test_purge_by_email_auth_delete_failure_502_touches_no_local_rows(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same guarantee as the by-id route (issue #225): an Auth-delete
    failure leaves every local row untouched — clean no-op, safe retry."""
    db_session.add(_user(_A, "a@example.com"))
    db_session.add(_h(user_id=_A, name="NVIDIA", ticker="NVDA"))
    db_session.flush()
    monkeypatch.setattr(
        "app.routers.admin.delete_auth_user",
        MagicMock(side_effect=AuthProviderError("boom")),
    )
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "a@example.com", "confirm": "a@example.com"},
    )
    assert resp.status_code == 502
    db_session.expire_all()
    assert db_session.get(User, _A) is not None
    assert _count(db_session, Holding.user_id, _A) == 1


def test_purge_by_email_orphan_auth_user_found(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local row gone, Supabase Auth account remains — the by-email
    equivalent of the by-id orphan path (issue #225 semantics). The
    response's user_id reports the resolved Auth account."""
    delete_mock = MagicMock(return_value=True)
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user_by_email",
        MagicMock(return_value=AuthUserInfo(id=str(_UNKNOWN), email="orphan@example.com")),
    )
    monkeypatch.setattr("app.routers.admin.delete_auth_user", delete_mock)
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "orphan@example.com", "confirm": "orphan@example.com"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_deleted"] is True
    assert body["email"] == "orphan@example.com"
    assert body["user_id"] == str(_UNKNOWN)
    assert body["deleted"] == {
        "news_surfaced": 0,
        "reports": 0,
        "holdings": 0,
        "accounts": 0,
        "upload_jobs": 0,
        "user_investment_context": 0,
        "email_verifications": 0,
        "invites_used_by_cleared": 0,
        "users_invited_by_cleared": 0,
        "users": 0,
    }
    delete_mock.assert_called_once_with(str(_UNKNOWN))


def test_purge_by_email_orphan_auth_user_not_found_404(
    app_client: TestClient, _fake_get_auth_user_by_email: MagicMock
) -> None:
    """Neither the local users table nor Supabase Auth has this email —
    the only case that still 404s."""
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "nobody@example.com", "confirm": "nobody@example.com"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "user not found"


def test_purge_by_email_orphan_confirm_mismatch_409(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive: the boundary already requires email == confirm, but the
    Supabase user's stored email is the second fact being checked (same
    contract as the by-id orphan path) — a lookup that somehow resolves to
    a different address must 409 before deleting anything."""
    delete_mock = MagicMock(return_value=True)
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user_by_email",
        MagicMock(return_value=AuthUserInfo(id=str(_UNKNOWN), email="other@example.com")),
    )
    monkeypatch.setattr("app.routers.admin.delete_auth_user", delete_mock)
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "a@example.com", "confirm": "a@example.com"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "confirm does not match user email"
    delete_mock.assert_not_called()


def test_purge_by_email_orphan_lookup_failure_502(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user_by_email",
        MagicMock(side_effect=AuthProviderError("boom")),
    )
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "orphan@example.com", "confirm": "orphan@example.com"},
    )
    assert resp.status_code == 502


def test_purge_by_email_orphan_delete_failure_502(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user_by_email",
        MagicMock(return_value=AuthUserInfo(id=str(_UNKNOWN), email="orphan@example.com")),
    )
    monkeypatch.setattr(
        "app.routers.admin.delete_auth_user",
        MagicMock(side_effect=AuthProviderError("boom")),
    )
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "orphan@example.com", "confirm": "orphan@example.com"},
    )
    assert resp.status_code == 502


def test_purge_by_email_orphan_auth_subject_bound_to_live_user_409(
    app_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Email drift reverse-orphan (review, PR #275): a live local row's
    `auth_subject` points at the Auth account the orphan lookup resolved
    under a different local email (Dashboard email change, or a row bound
    to an Auth user that later got this address). Deleting here would
    Auth-delete a live account while its local row stands — the same
    class of guard the by-id path 409s on (PR #246 round 2). Must 409
    before any Auth call."""
    user = _user(_A, "a@example.com")
    user.auth_subject = str(_AUTH_SUB)
    db_session.add(user)
    db_session.flush()
    delete_mock = MagicMock(return_value=True)
    monkeypatch.setattr(
        "app.routers.admin.get_auth_user_by_email",
        MagicMock(return_value=AuthUserInfo(id=str(_AUTH_SUB), email="drifted@example.com")),
    )
    monkeypatch.setattr("app.routers.admin.delete_auth_user", delete_mock)
    resp = app_client.delete(
        _by_email_path(),
        headers=_headers(),
        params={"email": "drifted@example.com", "confirm": "drifted@example.com"},
    )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert str(_A) in body
    assert "a@example.com" in body
    delete_mock.assert_not_called()
    db_session.expire_all()
    assert db_session.get(User, _A) is not None
