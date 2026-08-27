"""DELETE /admin/users/{user_id} hard purge (issue #199)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

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
from app.services.invites import hash_invite_token
from app.services.questionnaire_taxonomy import QUESTIONNAIRE_VERSION
from app.tests.test_admin_router import _headers
from app.tests.test_user_scope import _h, _user

_A = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_B = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
_UNKNOWN = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

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
