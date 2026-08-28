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
        "upload_jobs": 1,
        "user_investment_context": 1,
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
        "upload_jobs": 0,
        "user_investment_context": 0,
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
