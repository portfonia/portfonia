"""Hard-purge one user's own rows (issue #199, extended by #225 and B7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.api_audit_log import ApiAuditLog
from app.models.api_token import ApiToken
from app.models.credit_ledger import CreditLedgerEntry
from app.models.email_verification import EmailVerification
from app.models.holding import Holding
from app.models.invite import Invite
from app.models.news_surfaced import NewsSurfaced
from app.models.report import Report
from app.models.upload_job import UploadJob
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.models.waitlist_entry import WaitlistEntry


@dataclass(frozen=True)
class PurgeResult:
    waitlist_entries: int
    invite_emails_cleared: int
    api_audit_log: int
    api_tokens: int
    news_surfaced: int
    reports: int
    holdings: int
    accounts: int
    upload_jobs: int
    user_investment_context: int
    email_verifications: int
    invites_used_by_cleared: int
    users_invited_by_cleared: int
    users: int
    credit_ledger_flagged: int


def _rowcount(result: CursorResult[Any]) -> int:
    return int(result.rowcount)


def _normalize_email(email: str | None) -> str | None:
    if email is None:
        return None
    return email.strip().lower() or None


def refuse_protected_user(session: Session, user: User, *, seed_user_id: UUID) -> None:
    """Shared seed/invite refusals, in the existing Ops order."""
    if user.id == seed_user_id:
        raise HTTPException(status_code=409, detail="refusing to delete the seed user")
    if session.scalar(select(exists().where(Invite.created_by == user.id))):
        raise HTTPException(
            status_code=409, detail="user created invites; revoke or reassign first"
        )


def purge_user(session: Session, user_id: UUID) -> PurgeResult:
    """Delete one user's own rows. Caller commits. HTTP refusals stay in the router."""
    email = session.scalar(select(User.email).where(User.id == user_id))
    addr = _normalize_email(email)
    entry_ids = list(
        session.scalars(
            select(WaitlistEntry.id).where(func.lower(func.btrim(WaitlistEntry.email)) == addr)
        )
    )
    invite_emails_cleared = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(
                update(Invite)
                .where(
                    or_(
                        func.lower(func.btrim(Invite.email)) == addr,
                        Invite.used_by_user_id == user_id,
                        Invite.waitlist_entry_id.in_(entry_ids),
                    )
                )
                .values(email=None, waitlist_entry_id=None)
            ),
        )
    )
    waitlist_entries = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(delete(WaitlistEntry).where(WaitlistEntry.id.in_(entry_ids))),
        )
    )
    api_audit_log = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(delete(ApiAuditLog).where(ApiAuditLog.user_id == user_id)),
        )
    )
    api_tokens = _rowcount(
        cast(
            CursorResult[Any], session.execute(delete(ApiToken).where(ApiToken.user_id == user_id))
        )
    )
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
    # Must follow DELETE holdings: holdings.account_id FKs to accounts.id
    # (ON DELETE RESTRICT) — issue #129 checkpoint B7.
    accounts = _rowcount(
        cast(CursorResult[Any], session.execute(delete(Account).where(Account.user_id == user_id)))
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
    # Must precede DELETE users: email_verifications.user_id FKs to users.id
    # ON DELETE RESTRICT (issue #260) — same class of gap B7 already paid
    # for on holdings/reports/upload_jobs/news_surfaced (review, PR #261).
    # Unbound ops_manual probes (user_id NULL) are never touched here.
    email_verifications = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(delete(EmailVerification).where(EmailVerification.user_id == user_id)),
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
    credit_ledger_flagged = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(
                update(CreditLedgerEntry)
                .where(
                    CreditLedgerEntry.user_id == user_id,
                    CreditLedgerEntry.user_deleted_at.is_(None),
                )
                .values(user_deleted_at=func.now())
            ),
        )
    )
    users = _rowcount(
        cast(CursorResult[Any], session.execute(delete(User).where(User.id == user_id)))
    )
    return PurgeResult(
        waitlist_entries=waitlist_entries,
        invite_emails_cleared=invite_emails_cleared,
        api_audit_log=api_audit_log,
        api_tokens=api_tokens,
        news_surfaced=news_surfaced,
        reports=reports,
        holdings=holdings,
        accounts=accounts,
        upload_jobs=upload_jobs,
        user_investment_context=user_investment_context,
        email_verifications=email_verifications,
        invites_used_by_cleared=invites_used_by_cleared,
        users_invited_by_cleared=users_invited_by_cleared,
        users=users,
        credit_ledger_flagged=credit_ledger_flagged,
    )
