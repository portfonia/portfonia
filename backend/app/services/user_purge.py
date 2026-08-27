"""Hard-purge one user's own rows (issue #199)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.holding import Holding
from app.models.invite import Invite
from app.models.news_surfaced import NewsSurfaced
from app.models.report import Report
from app.models.upload_job import UploadJob
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext


@dataclass(frozen=True)
class PurgeResult:
    news_surfaced: int
    reports: int
    holdings: int
    upload_jobs: int
    user_investment_context: int
    invites_used_by_cleared: int
    users_invited_by_cleared: int
    users: int


def _rowcount(result: CursorResult[Any]) -> int:
    return int(result.rowcount)


def purge_user(session: Session, user_id: UUID) -> PurgeResult:
    """Delete one user's own rows. Caller commits. HTTP refusals stay in the router."""
    news_surfaced = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(delete(NewsSurfaced).where(NewsSurfaced.user_id == user_id)),
        )
    )
    reports = _rowcount(
        cast(CursorResult[Any], session.execute(delete(Report).where(Report.user_id == user_id)))
    )
    holdings = _rowcount(
        cast(CursorResult[Any], session.execute(delete(Holding).where(Holding.user_id == user_id)))
    )
    upload_jobs = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(delete(UploadJob).where(UploadJob.user_id == user_id)),
        )
    )
    # Must precede DELETE users: user_investment_context.user_id FKs to users.id.
    user_investment_context = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(
                delete(UserInvestmentContext).where(UserInvestmentContext.user_id == user_id)
            ),
        )
    )
    invites_used_by_cleared = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(
                update(Invite).where(Invite.used_by_user_id == user_id).values(used_by_user_id=None)
            ),
        )
    )
    users_invited_by_cleared = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(
                update(User)
                .where(User.invited_by == user_id, User.id != user_id)
                .values(invited_by=None)
            ),
        )
    )
    users = _rowcount(
        cast(CursorResult[Any], session.execute(delete(User).where(User.id == user_id)))
    )
    return PurgeResult(
        news_surfaced=news_surfaced,
        reports=reports,
        holdings=holdings,
        upload_jobs=upload_jobs,
        user_investment_context=user_investment_context,
        invites_used_by_cleared=invites_used_by_cleared,
        users_invited_by_cleared=users_invited_by_cleared,
        users=users,
    )
