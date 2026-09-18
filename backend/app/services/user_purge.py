"""Hard-purge one user's own rows (issue #199, extended by #225 and B7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.email_verification import EmailVerification
from app.models.holding import Holding
from app.models.invite import Invite
from app.models.news_surfaced import NewsSurfaced
from app.models.report import Report
from app.models.upload_job import UploadJob
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.models.vigil import (
    VigilConfiguration,
    VigilDeliveryEvent,
    VigilObject,
    VigilOutbox,
    VigilVault,
)


@dataclass(frozen=True)
class PurgeResult:
    news_surfaced: int
    reports: int
    holdings: int
    accounts: int
    upload_jobs: int
    user_investment_context: int
    email_verifications: int
    invites_used_by_cleared: int
    users_invited_by_cleared: int
    vigil_delivery_events: int
    vigil_outbox: int
    vigil_objects: int
    vigil_configurations: int
    vigil_vaults: int
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
    # Extends the #451 base-row purge hook for #454's two new tables
    # (Design section 3 / section 6 order: "Clear vault active/pending
    # references, then remove ... objects, configurations ... and vault").
    # vigil_vaults.active/pending_config_id/object_id are now real
    # ON DELETE RESTRICT FKs into these tables (#454) — nulling them out
    # first is required, not optional, or the DELETEs below fail loudly
    # rather than silently bypassing the feature's stop path.
    vault_id = session.execute(
        select(VigilVault.id).where(VigilVault.owner_user_id == user_id)
    ).scalar_one_or_none()
    vigil_delivery_events = 0
    vigil_outbox = 0
    vigil_objects = 0
    vigil_configurations = 0
    if vault_id is not None:
        session.execute(
            update(VigilVault)
            .where(VigilVault.id == vault_id)
            .values(
                active_config_id=None,
                active_object_id=None,
                pending_config_id=None,
                pending_object_id=None,
            )
        )
        # #457 (P3.2) extends the #456 purge hook: vigil_delivery_events
        # FKs into vigil_outbox (RESTRICT), so associated rows go first.
        # Unmatched events that already carry this vault's provider_id are
        # removed in the same statement so a webhook-before-response row
        # cannot outlive the outbox it would have folded into.
        outbox_ids = list(
            session.scalars(select(VigilOutbox.id).where(VigilOutbox.vault_id == vault_id)).all()
        )
        provider_ids = list(
            session.scalars(
                select(VigilOutbox.provider_id).where(
                    VigilOutbox.vault_id == vault_id, VigilOutbox.provider_id.isnot(None)
                )
            ).all()
        )
        if outbox_ids or provider_ids:
            conditions = []
            if outbox_ids:
                conditions.append(VigilDeliveryEvent.outbox_id.in_(outbox_ids))
            if provider_ids:
                conditions.append(VigilDeliveryEvent.provider_message_id.in_(provider_ids))
            vigil_delivery_events = _rowcount(
                cast(
                    CursorResult[Any],
                    session.execute(delete(VigilDeliveryEvent).where(or_(*conditions))),
                )
            )
        # #456 (P3.1) extends the #454 purge hook: vigil_outbox has RESTRICT
        # FKs into vigil_configurations/vigil_objects, so it must be deleted
        # BEFORE them (Design section 3: "cancel Vigil pending sends ...
        # remove Vigil child rows in FK order"). No cancellation semantics
        # here — a hard purge removes the rows outright rather than
        # transitioning them through `cancelled` first, since nothing will
        # ever read this user's outbox again.
        vigil_outbox = _rowcount(
            cast(
                CursorResult[Any],
                session.execute(delete(VigilOutbox).where(VigilOutbox.vault_id == vault_id)),
            )
        )
        vigil_objects = _rowcount(
            cast(
                CursorResult[Any],
                session.execute(delete(VigilObject).where(VigilObject.vault_id == vault_id)),
            )
        )
        vigil_configurations = _rowcount(
            cast(
                CursorResult[Any],
                session.execute(
                    delete(VigilConfiguration).where(VigilConfiguration.vault_id == vault_id)
                ),
            )
        )
    # Must precede DELETE users: vigil_vaults.owner_user_id FKs to users.id
    # ON DELETE RESTRICT (issue #451 checkpoint P1.1) — the base-row purge
    # hook.
    vigil_vaults = _rowcount(
        cast(
            CursorResult[Any],
            session.execute(delete(VigilVault).where(VigilVault.owner_user_id == user_id)),
        )
    )
    users = _rowcount(
        cast(CursorResult[Any], session.execute(delete(User).where(User.id == user_id)))
    )
    return PurgeResult(
        news_surfaced=news_surfaced,
        reports=reports,
        holdings=holdings,
        accounts=accounts,
        upload_jobs=upload_jobs,
        user_investment_context=user_investment_context,
        email_verifications=email_verifications,
        invites_used_by_cleared=invites_used_by_cleared,
        users_invited_by_cleared=users_invited_by_cleared,
        vigil_delivery_events=vigil_delivery_events,
        vigil_outbox=vigil_outbox,
        vigil_objects=vigil_objects,
        vigil_configurations=vigil_configurations,
        vigil_vaults=vigil_vaults,
        users=users,
    )
