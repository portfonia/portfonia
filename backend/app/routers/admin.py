"""Ops API channel (issue #129 Ring 1 stage B, checkpoint B2).

An independent management surface, authenticated by `ADMIN_API_TOKEN` (a
static bearer secret) rather than the user auth system — Ring 1-B design.md
§4.3 spells out why the two must never merge: this channel has to keep
working when the login system itself is what's broken.

Convention established here for every later checkpoint (§4.5): anything
with an administrative purpose ships first as an `/admin/*` endpoint. A
management UI, if one ever exists, sits on top of these endpoints — it is
never a prerequisite for the capability existing.

`dependencies=[Depends(require_ops_token)]` is declared on the router
itself, not per-endpoint, so a new admin route is protected by construction
and can't ship unauthenticated by a missed `Depends(...)` at the call site
(B-UAT-4 locks this with a structural test over `app.routes`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import httpx
import openai
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.deps import require_ops_token
from app.core.ops_log import log_ops_event
from app.core.rate_limit import (
    check_report_resend_cooldown,
    rate_limit_create_invite,
    release_report_resend_cooldown,
)
from app.core.timezones import ET, today_et
from app.models.api_audit_log import ApiAuditLog
from app.models.email_verification import EmailVerification
from app.models.holding import Holding
from app.models.report import Report
from app.models.report_currency_change import ReportCurrencyChange
from app.models.report_job import ReportJob
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.models.waitlist_entry import WaitlistEntry
from app.schemas.agent import ApiAuditOut, ApiAuditPage, RevokedTokensOut
from app.schemas.holdings import VALID_CURRENCIES
from app.schemas.reports import GenerateReportRequest, ReportJobOut
from app.services import fx_fetcher, price_fetcher
from app.services.api_tokens import revoke_all as revoke_all_api_tokens
from app.services.auth_provider import (
    AuthProviderError,
    AuthUserInfo,
    delete_auth_user,
    get_auth_user,
    get_auth_user_by_email,
)
from app.services.credit_ledger import (
    IdempotencyConflict,
    InsufficientCredits,
    RefundExceedsPurchase,
    RefundWindowClosed,
    adjust_by_admin,
    debit_refund,
    purchase_refundable,
    refund_key,
)
from app.services.credit_ledger_export import build_balances_csv, build_ledger_csv
from app.services.email_sender import send_ops_alert, send_report_email
from app.services.email_verification import (
    ResendTooSoon,
    VerificationSendFailed,
    create_verification,
)
from app.services.fund_nav_fetcher import update_fund_navs
from app.services.invitation_letters import (
    LetterConflict,
    LetterSendFailed,
    _revoke_waitlist_links,
    send_letter,
)
from app.services.invites import (
    EmailAlreadyRegistered,
    create_invite,
    list_invites,
    revoke_invite,
)
from app.services.llm_errors import LLMEmptyResponseError
from app.services.paddle_client import (
    PaddleApiError,
    PaddleNotConfigured,
    create_refund_adjustment,
    get_transaction,
)
from app.services.report_currency import apply_report_currency_change
from app.services.report_generator import (
    regenerate_report,
)
from app.services.report_jobs import _job_out
from app.services.snapshot_recovery import (
    CATCHUP_LOOKBACK_DAYS,
    recover_portfolio_snapshots,
)
from app.services.ticker_leverage import (
    LeverageOverride,
    LeverageOverrideAlreadyExists,
    create_leverage_override,
    delete_leverage_override,
    get_leverage_override,
    list_leverage_overrides,
    update_leverage_override,
)
from app.services.user_directory import recipient_email_with_purpose
from app.services.user_purge import _normalize_email, purge_user, refuse_protected_user
from app.services.user_scope import report_currency_for, report_language_for
from app.services.waitlist import view as waitlist_view
from app.services.waitlist import views as waitlist_views
from app.tasks.admin_tasks import send_admin_alert_task
from app.tasks.report_tasks import generate_report_job

logger = logging.getLogger(__name__)

# A run of this many consecutive 401s on /admin/* is a real signal (nobody
# stumbles onto this token by accident) — alert once per run, then keep
# counting from zero so a sustained guessing attempt doesn't resend the
# alert on every single subsequent request (design doc §4.4.4).
_CONSECUTIVE_401_ALERT_THRESHOLD = 5
_consecutive_401_count = 0


class AdminLoggingRoute(APIRoute):
    """Audits every /admin/* call: endpoint, params, result, duration.

    Never logs the Authorization header or the token value — httpx's INFO
    logging of query strings already cost this repo a real leak once (see
    app/main.py's httpx log-level comment); this is the same class of
    mistake, guarded against from the start instead of patched after.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def logged_handler(request: Request) -> Response:
            global _consecutive_401_count
            start = time.monotonic()
            status_code = 500
            try:
                response = await original_handler(request)
                status_code = response.status_code
                return response
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            finally:
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                log_ops_event(
                    "admin.call",
                    endpoint=request.url.path,
                    method=request.method,
                    params=dict(request.path_params) | dict(request.query_params),
                    status_code=status_code,
                    duration_ms=duration_ms,
                )
                if status_code == 401:
                    _consecutive_401_count += 1
                    if _consecutive_401_count % _CONSECUTIVE_401_ALERT_THRESHOLD == 0:
                        # .delay() only enqueues (a fast Redis write) — the
                        # actual blocking send_ops_alert() call happens in a
                        # separate Celery worker process, never on this
                        # request's event loop (PR #177 review round 3).
                        # Isolated in its own try/except: a broker outage
                        # must never turn this already-decided 401 into a
                        # 500 (PR #177 review round 4 — reproduced: an
                        # unhandled exception here previously replaced the
                        # HTTPException already propagating from `except`
                        # above, since it's raised inside this `finally`).
                        try:
                            send_admin_alert_task.delay(
                                "Portfonia ops: repeated /admin unauthorized attempts",
                                f"{_consecutive_401_count} consecutive unauthorized "
                                f"/admin/* requests, most recently {request.method} "
                                f"{request.url.path}. No legitimate caller should ever "
                                "guess wrong this many times in a row.",
                            )
                        except Exception:
                            logger.exception(
                                "AdminLoggingRoute: failed to enqueue repeated-401 ops alert"
                            )
                else:
                    _consecutive_401_count = 0

        return logged_handler


router = APIRouter(route_class=AdminLoggingRoute, dependencies=[Depends(require_ops_token)])


@router.get("/credits/ledger.csv")
def export_credit_ledger(session: Session = Depends(get_session)) -> Response:
    return Response(
        content=build_ledger_csv(session),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=credit-ledger-{today_et()}.csv"},
    )


@router.get("/credits/balances.csv")
def export_credit_balances(session: Session = Depends(get_session)) -> Response:
    return Response(
        content=build_balances_csv(session),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=credit-balances-{today_et()}.csv"},
    )


class RefreshResult(BaseModel):
    prices_updated: int
    prices_failed: list[str]
    funds_updated: int
    funds_failed: list[str]
    fx_upserted: int
    fx_failed: list[str]


@router.post("/portfolio/refresh", response_model=RefreshResult)
def refresh_market_data(session: Session = Depends(get_session)) -> RefreshResult:
    """Manually trigger price / NAV / FX refresh.

    Moved from POST /portfolio/refresh (decision point 8/11): this pulls
    fresh market data for every user's holdings at once, so it's an ops
    action, not something an individual user should be able to trigger.
    """
    prices = price_fetcher.update_holding_prices(session)
    funds = update_fund_navs(session)
    fx = fx_fetcher.update_fx_rates(session)
    session.commit()
    return RefreshResult(
        prices_updated=prices.updated,
        prices_failed=prices.failed,
        funds_updated=funds.updated,
        funds_failed=funds.failed,
        fx_upserted=fx.upserted,
        fx_failed=fx.failed,
    )


class SnapshotRecoveryResult(BaseModel):
    replayed: int
    recomputed: int
    already_complete: int
    skipped_old: int
    skipped_deps: int
    failed: int
    dates: list[str]


@router.post("/portfolio/snapshots/recover", response_model=SnapshotRecoveryResult)
def recover_portfolio_snapshots_endpoint(
    start_date: date | None = None,
    end_date: date | None = None,
    session: Session = Depends(get_session),
) -> SnapshotRecoveryResult:
    """Recover Portfolio Performance snapshot days (issue #373).

    Replays every frozen-but-unpublished payload in the window, and rebuilds
    any remaining missing day from live holdings unconditionally as long as
    it is inside the short catch-up window (issue #497 removed the
    composition-fingerprint gate this used to require). Days it still won't
    touch are reported (`skipped_old` / `skipped_deps` / `failed`) and
    logged.

    Window defaults to the last `CATCHUP_LOOKBACK_DAYS` ending today, and is
    capped at `MAX_RECOVERY_WINDOW_DAYS` per request because it runs inline.
    """
    end = end_date or today_et()
    start = start_date or end - timedelta(days=CATCHUP_LOOKBACK_DAYS)
    try:
        report = recover_portfolio_snapshots(session, start, end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return SnapshotRecoveryResult(
        replayed=report.replayed,
        recomputed=report.recomputed,
        already_complete=report.already_complete,
        skipped_old=report.skipped_old,
        skipped_deps=report.skipped_deps,
        failed=report.failed,
        dates=list(report.dates),
    )


class CreateInviteBody(BaseModel):
    email: str | None = None
    expires_days: int = Field(default=14, ge=1, le=90)


class InvitationLetterBody(BaseModel):
    email: str
    language: Literal["en", "zh", "zh-Hant"] | None = None
    expires_days: int = Field(default=14, ge=1, le=90)


class InvitationLetterOut(BaseModel):
    invite_id: UUID
    email: str
    language: Literal["en", "zh", "zh-Hant"]
    waitlist_entry_id: UUID | None
    expires_at: datetime
    invite_url: str
    letter_sent_at: datetime
    provider_message_id: str


@router.post("/invitation-letters", response_model=InvitationLetterOut, status_code=201)
def send_invitation_letter_endpoint(
    body: InvitationLetterBody,
    session: Session = Depends(get_session),
    _: None = Depends(rate_limit_create_invite),
) -> InvitationLetterOut:
    if not body.email.strip().lower():
        raise HTTPException(status_code=422, detail="email is required")
    try:
        result = send_letter(
            session,
            body.email,
            language=body.language,
            expires_days=body.expires_days,
            created_by=UUID(get_settings().ADMIN_ID),
            require_pending=False,
        )
    except LetterConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from None
    except LetterSendFailed:
        raise HTTPException(
            status_code=502,
            detail="invitation email send failed; no invite was saved",
        ) from None
    return InvitationLetterOut(
        invite_id=result.invite_id,
        email=result.email,
        language=result.language,
        waitlist_entry_id=result.waitlist_entry_id,
        expires_at=result.expires_at,
        invite_url=result.invite_url,
        letter_sent_at=result.letter_sent_at,
        provider_message_id=result.provider_message_id,
    )


class WaitlistEntryOut(BaseModel):
    id: UUID
    email: str
    locale: str
    stage: Literal["pending", "invited", "sent", "registered", "activated", "rejected"]
    status: Literal["pending", "invited", "rejected"]
    created_at: datetime
    status_changed_at: datetime
    link_generated_at: datetime | None
    link_expires_at: datetime | None
    link_expired: bool
    link_sent_at: datetime | None
    registered_at: datetime | None
    verified_at: datetime | None
    activated_at: datetime | None
    user_id: UUID | None


class WaitlistInviteOut(WaitlistEntryOut):
    token: str
    invite_url: str


class WaitlistInviteBody(BaseModel):
    expires_days: int = Field(default=14, ge=1, le=90)


class WaitlistStatusBody(BaseModel):
    status: Literal["pending", "rejected"]


def _waitlist_entry(session: Session, entry_id: UUID, *, lock: bool = False) -> WaitlistEntry:
    query = select(WaitlistEntry).where(WaitlistEntry.id == entry_id)
    if lock:
        # Keep at most one active invite per entry across concurrent requests.
        query = query.with_for_update()
    entry = session.scalar(query)
    if entry is None:
        raise HTTPException(status_code=404, detail="waitlist entry not found")
    return entry


@router.get("/waitlist", response_model=list[WaitlistEntryOut])
def list_waitlist(
    stage: Literal["pending", "invited", "sent", "registered", "activated", "rejected"]
    | None = None,
    link_expired: bool | None = None,
    session: Session = Depends(get_session),
) -> list[WaitlistEntryOut]:
    entries = list(session.scalars(select(WaitlistEntry).order_by(WaitlistEntry.created_at.desc())))
    states = waitlist_views(session, entries)
    return [
        WaitlistEntryOut.model_validate(state)
        for state in states
        if (stage is None or state["stage"] == stage)
        and (link_expired is None or state["link_expired"] == link_expired)
    ]


@router.get("/waitlist/by-email", response_model=WaitlistEntryOut)
def get_waitlist_by_email(email: str, session: Session = Depends(get_session)) -> WaitlistEntryOut:
    entry = session.scalar(
        select(WaitlistEntry).where(WaitlistEntry.email == email.strip().lower())
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="waitlist entry not found")
    return WaitlistEntryOut.model_validate(waitlist_view(session, entry))


@router.get("/waitlist/{entry_id}", response_model=WaitlistEntryOut)
def get_waitlist(entry_id: UUID, session: Session = Depends(get_session)) -> WaitlistEntryOut:
    return WaitlistEntryOut.model_validate(
        waitlist_view(session, _waitlist_entry(session, entry_id))
    )


@router.post("/waitlist/{entry_id}/invite", response_model=WaitlistInviteOut)
def mint_waitlist_invite(
    entry_id: UUID,
    body: WaitlistInviteBody,
    session: Session = Depends(get_session),
    _: None = Depends(rate_limit_create_invite),
) -> WaitlistInviteOut:
    entry = _waitlist_entry(session, entry_id, lock=True)
    state = waitlist_view(session, entry)
    if state["stage"] in ("registered", "activated"):
        raise HTTPException(status_code=409, detail="entry already registered")
    now = datetime.now(tz=ET)
    _revoke_waitlist_links(session, entry_id, now)
    try:
        issued = create_invite(
            session,
            created_by=UUID(get_settings().ADMIN_ID),
            email=entry.email,
            expires_days=body.expires_days,
            waitlist_entry_id=entry.id,
        )
    except EmailAlreadyRegistered:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="email already belongs to an existing user"
        ) from None
    entry.status = "invited"
    entry.link_sent_at = None
    entry.status_changed_at = now
    session.commit()
    state = waitlist_view(session, entry)
    return WaitlistInviteOut.model_validate(
        {
            **state,
            "token": issued.token,
            "invite_url": f"{get_settings().FRONTEND_URL}/signup?invite={issued.token}",
        }
    )


@router.post("/waitlist/{entry_id}/sent", response_model=WaitlistEntryOut)
def mark_waitlist_sent(entry_id: UUID, session: Session = Depends(get_session)) -> WaitlistEntryOut:
    entry = _waitlist_entry(session, entry_id, lock=True)
    state = waitlist_view(session, entry)
    if state["stage"] != "invited" or state["link_expired"]:
        raise HTTPException(status_code=409, detail=f"current stage: {state['stage']}")
    entry.link_sent_at = datetime.now(tz=ET)
    session.commit()
    return WaitlistEntryOut.model_validate(waitlist_view(session, entry))


@router.patch("/waitlist/{entry_id}/status", response_model=WaitlistEntryOut)
def set_waitlist_status(
    entry_id: UUID,
    body: WaitlistStatusBody,
    session: Session = Depends(get_session),
) -> WaitlistEntryOut:
    entry = _waitlist_entry(session, entry_id, lock=True)
    state = waitlist_view(session, entry)
    if state["stage"] in ("registered", "activated"):
        raise HTTPException(status_code=409, detail="entry already registered")
    now = datetime.now(tz=ET)
    _revoke_waitlist_links(session, entry_id, now)
    entry.status = body.status
    entry.link_sent_at = None
    entry.status_changed_at = now
    session.commit()
    return WaitlistEntryOut.model_validate(waitlist_view(session, entry))


class InviteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str | None
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    token: str | None = None


@router.post("/invites", response_model=InviteOut, status_code=201)
def create_invite_endpoint(
    body: CreateInviteBody,
    session: Session = Depends(get_session),
    _: None = Depends(rate_limit_create_invite),
) -> InviteOut:
    try:
        issued = create_invite(
            session,
            created_by=UUID(get_settings().ADMIN_ID),
            email=body.email,
            expires_days=body.expires_days,
        )
    except EmailAlreadyRegistered:
        # Issue #188: fail at creation instead of a token that can only
        # die generically at redemption.
        raise HTTPException(
            status_code=409, detail="email already belongs to an existing user"
        ) from None
    session.commit()
    return InviteOut(
        id=issued.id,
        email=issued.email,
        expires_at=issued.expires_at,
        used_at=None,
        revoked_at=None,
        created_at=issued.created_at,
        token=issued.token,
    )


@router.get("/invites", response_model=list[InviteOut])
def list_invites_endpoint(session: Session = Depends(get_session)) -> list[InviteOut]:
    return [InviteOut.model_validate(row) for row in list_invites(session)]


@router.delete("/invites/{invite_id}", status_code=204)
def revoke_invite_endpoint(invite_id: UUID, session: Session = Depends(get_session)) -> None:
    try:
        revoke_invite(session, invite_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="invite not found") from None
    session.commit()


class BindSubjectBody(BaseModel):
    auth_subject: str = Field(min_length=1)

    @field_validator("auth_subject")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("auth_subject must not be blank")
        return stripped


class BindSubjectOut(BaseModel):
    id: UUID
    auth_subject: str


@router.post("/users/{user_id}/bind-subject", response_model=BindSubjectOut)
def bind_user_subject(
    user_id: UUID, body: BindSubjectBody, session: Session = Depends(get_session)
) -> BindSubjectOut:
    """Attach a Supabase Auth `sub` to a users row that has none.

    Needed for the production seed row (`auth_subject` is NULL after the
    B4 migration). Does not insert users and will not overwrite a bound sub.
    """
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.auth_subject is not None:
        raise HTTPException(status_code=409, detail="auth_subject already set")
    user.auth_subject = body.auth_subject
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="auth_subject already bound") from None
    return BindSubjectOut(id=user.id, auth_subject=user.auth_subject)


class UpdateCadenceBody(BaseModel):
    # Literal, not a bare str + DB CheckConstraint fallback: a bad value gets
    # a clean 422 here rather than an IntegrityError bubbling into a 500 —
    # matters more once this endpoint is reused for self-service cadence
    # changes (issue #191), not just ops calls. Keep in sync with
    # app.models.user.VALID_REPORT_CADENCES by hand; Pydantic Literal
    # members must be compile-time, not derived from that tuple.
    report_cadence: Literal["daily", "mwf", "weekly"]


class UpdateCadenceOut(BaseModel):
    id: UUID
    email: str
    report_cadence: str


@router.post("/users/{user_id}/cadence", response_model=UpdateCadenceOut)
def update_user_cadence(
    user_id: UUID, body: UpdateCadenceBody, session: Session = Depends(get_session)
) -> UpdateCadenceOut:
    """Cadence follows the subscription; Ops writes are suspended (#595)."""
    raise HTTPException(
        status_code=409, detail="cadence changes are suspended; cadence follows the subscription"
    )


# Literal members must be compile-time, so these are hand-kept copies of
# app.models.user.VALID_USER_STATUSES / VALID_REPORT_CADENCES — same
# discipline as UpdateCadenceBody (PR #248); a drift test over both copies
# lives in test_admin_users.py.
UserStatusFilter = Literal["active", "deleted", "suspended"]
ReportCadenceFilter = Literal["daily", "mwf", "none", "weekly"]


class UserSummaryOut(BaseModel):
    """One account's basic facts — deliberately NOT the full PurgeUserOut
    shape (this is read-only; there is no `deleted{}` block)."""

    id: UUID
    email: str
    status: str
    created_at: datetime
    report_cadence: str
    auth_subject_bound: bool
    has_investment_context: bool
    holdings_count: int
    subscription_status: str
    subscription_type: str | None
    subscription_expires_on: date | None
    subscription_cancel_pending: bool


@router.get("/users", response_model=list[UserSummaryOut])
def list_users_endpoint(
    email: str | None = None,
    status: Annotated[UserStatusFilter | None, Query()] = None,
    report_cadence: Annotated[ReportCadenceFilter | None, Query()] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[UserSummaryOut]:
    """Read-only ops user directory (issue #278).

    Why this exists: after issue #274/PR #275, the delete-by-email
    pre-delete confirmation policy must report an account's facts
    (`created_at`, whether it has questionnaire/investment-context data,
    holdings count) and get a human re-confirmation before deleting — and
    before this endpoint, satisfying that policy still required SSH+psql,
    the exact step #274 was built to remove. This is that policy's read
    path: resolve an email to a user_id with enough context to know the
    right account was found. Not a generic "list users for troubleshooting"
    surface, and GET-only by design — no write path here.

    All query params are optional. `email` is exact-match only after
    `_normalize_email` (strip + lowercase, same as signup); whitespace-only
    input normalizes to None and behaves like the param being absent.
    `status`/`report_cadence` reject values outside their legal sets with
    the same 422 shape as the cadence endpoint's Literal. `limit`/`offset`
    page the unfiltered/broad-filter case (default 50, capped 200).
    """
    normalized_email = _normalize_email(email)
    holdings_count = (
        select(func.count())
        .select_from(Holding)
        .where(Holding.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    has_context = (
        select(func.count())
        .select_from(UserInvestmentContext)
        .where(UserInvestmentContext.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    stmt = select(
        User,
        holdings_count.label("holdings_count"),
        has_context.label("has_investment_context"),
    )
    if normalized_email is not None:
        stmt = stmt.where(User.email == normalized_email)
    if status is not None:
        stmt = stmt.where(User.status == status)
    if report_cadence is not None:
        stmt = stmt.where(User.report_cadence == report_cadence)
    stmt = stmt.order_by(User.created_at, User.id).limit(limit).offset(offset)

    rows = session.execute(stmt).all()
    return [
        UserSummaryOut(
            id=user.id,
            email=user.email,
            status=user.status,
            created_at=user.created_at,
            report_cadence=user.report_cadence,
            auth_subject_bound=user.auth_subject is not None,
            has_investment_context=has_context_count > 0,
            holdings_count=holdings_count_value,
            subscription_status=user.subscription_status,
            subscription_type=user.subscription_type,
            subscription_expires_on=user.subscription_expires_on,
            subscription_cancel_pending=user.subscription_cancel_pending,
        )
        for user, holdings_count_value, has_context_count in rows
    ]


@router.post("/users/{user_id}/reports/generate", response_model=ReportJobOut, status_code=202)
def enqueue_report_for_user(
    user_id: UUID,
    req: GenerateReportRequest | None = None,
    session: Session = Depends(get_session),
) -> ReportJobOut:
    """Accept an Ops report generation; personalization is resolved by the worker."""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.status != "active":
        raise HTTPException(status_code=422, detail="user is not active")
    req = req or GenerateReportRequest()
    job = ReportJob(user_id=user_id, status="pending")
    session.add(job)
    session.commit()
    session.refresh(job)
    try:
        generate_report_job.delay(
            str(job.id),
            report_type=req.report_type,
            report_date=req.report_date.isoformat() if req.report_date is not None else None,
            base_currency=req.base_currency,
            session_node=req.session_node,
        )
    except Exception as exc:
        logger.exception("enqueue_report_for_user: failed to enqueue job %s", job.id)
        job.status = "failed"
        job.error = f"Failed to queue report job: {type(exc).__name__}: {exc}"
        session.commit()
        raise HTTPException(
            status_code=503, detail="Could not queue the report generation. Please try again."
        ) from exc
    return _job_out(session, job)


@router.get("/report-jobs/{job_id}", response_model=ReportJobOut)
def get_report_job_for_ops(job_id: UUID, session: Session = Depends(get_session)) -> ReportJobOut:
    job = session.get(ReportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Report job not found.")
    return _job_out(session, job)


@router.post("/users/{user_id}/reports/{report_id}/send")
def send_report_for_user(
    user_id: UUID, report_id: UUID, session: Session = Depends(get_session)
) -> dict[str, str | None]:
    report = session.scalar(select(Report).where(Report.id == report_id, Report.user_id == user_id))
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.status != "success":
        raise HTTPException(status_code=422, detail="Report is not in success state")
    if report.email_sent_at is not None:
        return {"status": "already_sent", "email_sent_at": report.email_sent_at.isoformat()}
    if not send_report_email(report, session):
        raise HTTPException(status_code=502, detail="Email delivery failed — check server logs")
    return {
        "status": "sent",
        "email_sent_at": report.email_sent_at.isoformat() if report.email_sent_at else None,
    }


def _release_report_resend_cooldown_if_claimed(email: str | None) -> None:
    """PR #338 review leftover (blacktomb42): a claimed cooldown must not
    outlive a `regenerate_report` failure that happened before any resend
    was actually attempted — no-ops when `email` is None (nothing was
    claimed, e.g. no verified recipient)."""
    if email is not None:
        release_report_resend_cooldown(email)


class RerunReportRequest(BaseModel):
    mode: Literal["analyze", "render"] = "analyze"
    resend: bool = True


class ReportRerunOut(BaseModel):
    report_id: UUID
    user_id: UUID
    status: str
    mode: str
    email_sent_at: datetime | None
    provider_message_id: str | None


@router.post(
    "/users/{user_id}/reports/{report_id}/rerun",
    response_model=ReportRerunOut,
)
def rerun_report_for_user(
    user_id: UUID,
    report_id: UUID,
    req: RerunReportRequest,
    session: Session = Depends(get_session),
) -> ReportRerunOut:
    """Rerun (and optionally resend) one already-generated report (issue #324).

    Built for the "holdings were corrected after a report already shipped"
    case: rebuild the body from stored `report_inputs` — never re-fetching
    news/Tavily/macro intel — and, if requested, actually redeliver it.

    mode='analyze' (default) re-runs the body pass against a FRESH read of
    the user's live holdings via `regenerate_report` — this is what actually
    picks up a holdings/asset_class fix. mode='render' only re-renders the
    stored body (formatting/output-language iteration).

    resend=true (default): clears `email_sent_at`/`provider_message_id` on
    the target row BEFORE calling `regenerate_report`, then — only if the
    resulting status is "success" — explicitly calls `send_report_email`.
    Clearing first matters: `send_report_email`'s G3 dedup guard silently
    no-ops on any report where `email_sent_at` is already set, so without
    this step a rerun would produce a corrected body that never actually
    goes out. resend=false leaves `email_sent_at` untouched and never sends.

    The `Report` row is never physically deleted: doing so would destroy
    the `report_inputs` JSONB cache that makes a no-refetch rerun possible
    in the first place.
    """
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    report = session.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user_id)
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")

    claimed_cooldown_email: str | None = None
    if req.resend:
        # issue #104 requirement #6: 15-minute manual-resend cooldown, keyed
        # by recipient address — checked BEFORE clearing email_sent_at/
        # provider_message_id below, because that clearing (not just the
        # eventual send) is the actual overwrite poll_report_delivery's
        # 10-minute-delayed read of THIS row needs protection from. Skipped
        # entirely when there's no verified address to resolve — nothing
        # will be sent in that case either way (send_report_email's own
        # fail-closed handling), so there's no overwrite risk to guard.
        resolved = recipient_email_with_purpose(session, user_id)
        if resolved is not None:
            recipient_email, _purpose = resolved
            remaining = check_report_resend_cooldown(recipient_email)
            if remaining is not None:
                raise HTTPException(
                    status_code=429,
                    detail=f"manual resend cooldown active, retry in {remaining}s",
                )
            claimed_cooldown_email = recipient_email
        report.email_sent_at = None
        report.provider_message_id = None
        session.flush()

    try:
        report = regenerate_report(
            session,
            report_id,
            user_id=user_id,
            mode=req.mode,
            output_lang=report_language_for(session, user_id, get_settings().OUTPUT_LANG),
            # Issue #350 item 1: this user's CURRENT base_currency
            # preference, not whatever the report was originally generated
            # with — mirrors output_lang immediately above. Only affects
            # mode="analyze" (regenerate_report's own re-fetch branch);
            # mode="render" never re-reads this parameter.
            base_currency=report_currency_for(session, user_id, "USD"),
        )
    except ValueError as exc:
        _release_report_resend_cooldown_if_claimed(claimed_cooldown_email)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMEmptyResponseError as exc:
        _release_report_resend_cooldown_if_claimed(claimed_cooldown_email)
        raise HTTPException(
            status_code=502, detail=f"LLM returned an empty response: {exc}"
        ) from exc
    except openai.APIError as exc:
        _release_report_resend_cooldown_if_claimed(claimed_cooldown_email)
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    except RuntimeError as exc:
        _release_report_resend_cooldown_if_claimed(claimed_cooldown_email)
        raise HTTPException(status_code=502, detail=f"Report regeneration failed: {exc}") from exc

    if req.resend and report.status == "success":
        send_report_email(report, session)

    return ReportRerunOut(
        report_id=report.id,
        user_id=report.user_id,
        status=report.status,
        mode=req.mode,
        email_sent_at=report.email_sent_at,
        provider_message_id=report.provider_message_id,
    )


class PurgeDeletedCounts(BaseModel):
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


_NO_LOCAL_ROWS = PurgeDeletedCounts(
    waitlist_entries=0,
    invite_emails_cleared=0,
    api_audit_log=0,
    api_tokens=0,
    news_surfaced=0,
    reports=0,
    holdings=0,
    accounts=0,
    upload_jobs=0,
    user_investment_context=0,
    email_verifications=0,
    invites_used_by_cleared=0,
    users_invited_by_cleared=0,
    users=0,
    credit_ledger_flagged=0,
)


class PurgeUserOut(BaseModel):
    user_id: UUID
    email: str
    # True only when an Auth user was actually found and removed from
    # Supabase (issue #225). False both when the local row had no
    # `auth_subject` to begin with, and when it did but Supabase already had
    # nothing there (a prior partial cleanup) — either way there was no live
    # Auth account for this call to remove.
    auth_deleted: bool
    deleted: PurgeDeletedCounts


def _auth_delete_or_502(sub: str) -> bool:
    """Delete the Supabase Auth user, mapping a provider failure to 502.

    Called before any local delete (issue #225 requirement A.2): nothing
    local has been touched yet at this point, so a 502 here means the whole
    request is a clean no-op — safe to retry, never a half purge.
    """
    try:
        return delete_auth_user(sub)
    except AuthProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail="failed to delete Supabase Auth user; local data not touched, retry",
        ) from exc


def _get_auth_user_or_502(sub: str) -> AuthUserInfo | None:
    """Same 502 mapping as `_auth_delete_or_502` (review, PR #246 round 1:
    this GET previously had no AuthProviderError mapping at all, so a
    GoTrue 5xx/timeout/malformed body surfaced as an unhandled 500 instead
    of the documented, retry-safe 502)."""
    try:
        return get_auth_user(sub)
    except AuthProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail="failed to look up Supabase Auth user; local data not touched, retry",
        ) from exc


def _purge_orphan_auth_user(user_id: UUID, confirm: str | None) -> PurgeUserOut:
    """Requirement B: local `users` row already gone, Supabase Auth account
    remains. Supabase Auth user ids are UUIDs, same shape as `user_id`.

    The caller has already ruled out `user_id` being some live user's
    `auth_subject` (round 2 review) — reaching here means neither a local
    PK nor a local auth_subject match exists, so a hit on Auth genuinely is
    an orphan.
    """
    auth_user = _get_auth_user_or_502(str(user_id))
    if auth_user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if confirm is None:
        raise HTTPException(status_code=422, detail="confirm query param is required")
    if _normalize_email(confirm) != _normalize_email(auth_user.email):
        raise HTTPException(status_code=409, detail="confirm does not match user email")
    _auth_delete_or_502(auth_user.id)
    return PurgeUserOut(
        user_id=user_id,
        email=auth_user.email,
        auth_deleted=True,
        deleted=_NO_LOCAL_ROWS,
    )


def _purge_local_user(session: Session, user: User, confirm: str | None) -> PurgeUserOut:
    """Guards + Auth delete + ordered local purge for an already-resolved
    local `users` row. Shared by the by-id and by-email purge routes
    (issue #274) so the refusal order, confirm contract and 10-step delete
    sequence live in exactly one place — the by-id route's original
    behavior is preserved verbatim; the by-email route passes its (already
    boundary-validated) confirm through the same checks."""
    refuse_protected_user(session, user)
    if confirm is None:
        raise HTTPException(status_code=422, detail="confirm query param is required")
    if _normalize_email(confirm) != _normalize_email(user.email):
        raise HTTPException(status_code=409, detail="confirm does not match user email")

    if user.credit_cash_balance > 0:
        raise HTTPException(
            status_code=409,
            detail="user has a cash balance; refund or adjust it to zero first",
        )

    auth_deleted = False
    if user.auth_subject is not None:
        auth_deleted = _auth_delete_or_502(user.auth_subject)

    email = user.email
    result = purge_user(session, user.id)
    session.commit()
    return PurgeUserOut(
        user_id=user.id,
        email=email,
        auth_deleted=auth_deleted,
        deleted=PurgeDeletedCounts(
            waitlist_entries=result.waitlist_entries,
            invite_emails_cleared=result.invite_emails_cleared,
            api_audit_log=result.api_audit_log,
            api_tokens=result.api_tokens,
            news_surfaced=result.news_surfaced,
            reports=result.reports,
            holdings=result.holdings,
            accounts=result.accounts,
            upload_jobs=result.upload_jobs,
            user_investment_context=result.user_investment_context,
            email_verifications=result.email_verifications,
            invites_used_by_cleared=result.invites_used_by_cleared,
            users_invited_by_cleared=result.users_invited_by_cleared,
            users=result.users,
            credit_ledger_flagged=result.credit_ledger_flagged,
        ),
    )


def _get_auth_user_by_email_or_502(email: str) -> AuthUserInfo | None:
    """502 mapping for the by-email orphan lookup — same shape as
    `_get_auth_user_or_502`, so the by-email route inherits the by-id
    route's retry-safe error contract (issue #274)."""
    try:
        return get_auth_user_by_email(email)
    except AuthProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail="failed to look up Supabase Auth user; local data not touched, retry",
        ) from exc


@router.delete("/users/by-email", response_model=PurgeUserOut)
def purge_user_by_email_endpoint(
    session: Session = Depends(get_session),
    email: str | None = None,
    confirm: str | None = None,
) -> PurgeUserOut:
    """Hard-purge by email (issue #274): collapse "delete the account for
    someone@example.com" into a single documented admin call — no SSH +
    psql lookup step to turn the email into a user_id first. Sibling route
    to DELETE /users/{user_id}, which is unchanged; callers who already
    hold a user_id keep using the by-id route.

    `email` and `confirm` are both required query params carrying the same
    value, each normalized via `_normalize_email` (strip+lowercase) before
    comparison. This is a self-consistency repeat check, deliberately
    weaker than the by-id route's id/email cross-check — the email itself
    is the single fact the caller must get right, and an agent fills both
    params from one string it was given once (no second human keystroke).

    Resolution: local `users` row by normalized email, else the Supabase
    Auth orphan path (get_auth_user_by_email), else 404. The response's
    user_id reports which row was actually resolved and deleted."""
    if email is None or confirm is None:
        raise HTTPException(status_code=422, detail="email and confirm query params are required")
    normalized_email = _normalize_email(email)
    normalized_confirm = _normalize_email(confirm)
    if normalized_email is None or normalized_confirm is None:
        raise HTTPException(status_code=422, detail="email and confirm query params are required")
    if normalized_email != normalized_confirm:
        raise HTTPException(status_code=422, detail="email and confirm must match")

    user = session.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
    if user is not None:
        return _purge_local_user(session, user, confirm)

    auth_user = _get_auth_user_by_email_or_502(normalized_email)
    if auth_user is None:
        raise HTTPException(status_code=404, detail="user not found")
    # Email drift can orphan in the reverse direction: a live local row
    # whose `auth_subject` points at this Auth account under a different
    # local email (Dashboard email change, or a row bound to an Auth user
    # that later got this address). Local lookup by query email misses,
    # Auth lookup hits, and deleting here would Auth-delete a live
    # account while its local row stands — the same class of reverse
    # orphan the by-id path 409s on (PR #246 round 2). Occupancy of
    # `auth_subject` is not a local-row-scoped guard in the seed/
    # created_invites sense; it means "this Auth account still belongs to
    # a live local user". Check before any Auth call, not after.
    live_owner = session.execute(
        select(User).where(User.auth_subject == auth_user.id)
    ).scalar_one_or_none()
    if live_owner is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{auth_user.id} is a Supabase Auth subject already bound to a local "
                f"user ({live_owner.email}); use DELETE /admin/users/{live_owner.id} instead"
            ),
        )
    if _normalize_email(auth_user.email) != normalized_email:
        raise HTTPException(status_code=409, detail="confirm does not match user email")
    _auth_delete_or_502(auth_user.id)
    return PurgeUserOut(
        user_id=UUID(auth_user.id),
        email=auth_user.email,
        auth_deleted=True,
        deleted=_NO_LOCAL_ROWS,
    )


class CreditAdjustmentBody(BaseModel):
    bucket: Literal["gift", "cash"] = "gift"
    email: str
    amount: Decimal = Field(max_digits=12, decimal_places=2)
    note: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=100)
    reference: str | None = Field(default=None, max_length=200)

    @field_validator("note", "idempotency_key", mode="before")
    @classmethod
    def _strip_required(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("amount")
    @classmethod
    def _nonzero_amount(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("amount must not be zero")
        return value


class CreditLedgerEntryOut(BaseModel):
    id: int
    bucket: str
    amount: Decimal
    balance_after: Decimal
    reason: str
    actor_type: str
    idempotency_key: str
    note: str | None
    reference: str | None
    created_at: datetime


class CreditAdjustmentOut(BaseModel):
    user_id: UUID
    email: str
    replayed: bool
    entry: CreditLedgerEntryOut
    cash_balance: Decimal
    gift_balance: Decimal


class RefundBody(BaseModel):
    transaction_id: str = Field(pattern=r"^txn_")
    credits: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    note: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)

    @field_validator("note", "idempotency_key", mode="before")
    @classmethod
    def _strip_refund_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class RefundOut(BaseModel):
    transaction_id: str
    adjustment_id: str | None
    adjustment_status: str | None
    credits: Decimal
    refund_amount: str | None
    currency_code: str | None
    replayed: bool
    entry: CreditLedgerEntryOut


@router.post("/payments/refunds", response_model=RefundOut)
def refund_payment(
    body: RefundBody, session: Session = Depends(get_session)
) -> RefundOut | JSONResponse:
    try:
        write = debit_refund(
            session,
            transaction_id=body.transaction_id,
            credits=body.credits,
            request_key=body.idempotency_key,
            note=body.note,
        )
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="purchase not found") from exc
    except RefundWindowClosed as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="refund window closed") from exc
    except RefundExceedsPurchase as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="credits exceed unrefunded purchase") from exc
    except InsufficientCredits as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="insufficient cash balance") from exc
    except (IdempotencyConflict, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="idempotency_key already used for a different adjustment"
        ) from exc

    entry = write.entries[0]
    if write.replayed:
        return RefundOut(
            transaction_id=body.transaction_id,
            adjustment_id=entry.reference,
            adjustment_status=None,
            credits=body.credits,
            refund_amount=None,
            currency_code=None,
            replayed=True,
            entry=CreditLedgerEntryOut.model_validate(entry, from_attributes=True),
        )

    purchase, remaining_after_debit = purchase_refundable(session, body.transaction_id)
    try:
        transaction = get_transaction(body.transaction_id)
        details = transaction.get("details")
        details_obj = cast(dict[str, object], details) if isinstance(details, dict) else {}
        line_items = details_obj.get("line_items")
        if (
            transaction.get("status") != "completed"
            or not isinstance(line_items, list)
            or len(line_items) != 1
        ):
            session.rollback()
            raise HTTPException(status_code=409, detail="manual refund required")
        item = cast(dict[str, object], line_items[0])
        totals = item.get("totals")
        totals_obj = cast(dict[str, object], totals) if isinstance(totals, dict) else {}
        total = int(str(totals_obj.get("total")))
        full = body.credits == purchase.amount and remaining_after_debit == 0
        amount = (
            str(total)
            if full
            else str(
                int(
                    (Decimal(total) * body.credits / purchase.amount).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
            )
        )
        if amount == "0":
            session.rollback()
            raise HTTPException(status_code=409, detail="refund amount rounds to zero")
        adjustment = create_refund_adjustment(
            transaction_id=body.transaction_id,
            reason=f"portfonia-refund:{refund_key(body.transaction_id, body.idempotency_key)}",
            full=full,
            item_id=str(item.get("id")),
            amount=None if full else amount,
        )
    except PaddleNotConfigured as exc:
        session.rollback()
        raise HTTPException(status_code=503, detail="payments not configured") from exc
    except (PaddleApiError, httpx.HTTPError) as exc:
        session.rollback()
        code = exc.code if isinstance(exc, PaddleApiError) else None
        return JSONResponse(
            status_code=502, content={"detail": "paddle error", "paddle_code": code}
        )

    adjustment_id = str(adjustment.get("id"))
    entry.reference = adjustment_id
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        send_ops_alert(
            "Paddle refund ledger commit failed",
            f"adjustment={adjustment_id} transaction={body.transaction_id} credits={body.credits} error={type(exc).__name__}",
        )
        raise HTTPException(status_code=500, detail="refund commit failed") from exc
    return RefundOut(
        transaction_id=body.transaction_id,
        adjustment_id=adjustment_id,
        adjustment_status=str(adjustment.get("status")),
        credits=body.credits,
        refund_amount=amount,
        currency_code=str(transaction.get("currency_code")),
        replayed=False,
        entry=CreditLedgerEntryOut.model_validate(entry, from_attributes=True),
    )


@router.post("/users/by-email/credit-adjustments", response_model=CreditAdjustmentOut)
def credit_adjustment_by_email(
    body: CreditAdjustmentBody,
    session: Session = Depends(get_session),
) -> CreditAdjustmentOut:
    normalized_email = _normalize_email(body.email)
    user = session.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    try:
        result = adjust_by_admin(
            session,
            user_id=user.id,
            amount=body.amount,
            bucket=body.bucket,
            note=body.note,
            idempotency_key=body.idempotency_key,
            reference=body.reference,
        )
        session.commit()
    except InsufficientCredits as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="insufficient balance") from exc
    except (IdempotencyConflict, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="idempotency_key already used for a different adjustment",
        ) from exc
    entry = result.entries[0]
    return CreditAdjustmentOut(
        user_id=user.id,
        email=user.email,
        replayed=result.replayed,
        entry=CreditLedgerEntryOut(
            id=entry.id,
            bucket=entry.bucket,
            amount=entry.amount,
            balance_after=entry.balance_after,
            reason=entry.reason,
            actor_type=entry.actor_type,
            idempotency_key=entry.idempotency_key,
            note=entry.note,
            reference=entry.reference,
            created_at=entry.created_at,
        ),
        cash_balance=user.credit_cash_balance,
        gift_balance=user.credit_gift_balance,
    )


class UpdateReportLanguageByEmailBody(BaseModel):
    # Same Literal values as backend/app/routers/me.py's
    # UpdateReportLanguageBody — reusing the same whitelist and Literal per
    # the engineering contract ("do not define a second, separately
    # drifting whitelist for the ops path"). Keep in sync with
    # app.models.user.VALID_REPORT_LANGUAGES by hand; Pydantic Literal
    # members must be compile-time, not derived from that tuple.
    report_language: Literal["en", "zh", "zh-Hant"]


class UpdateReportLanguageByEmailOut(BaseModel):
    user_id: UUID
    email: str
    report_language: str


@router.post("/users/by-email/report-language", response_model=UpdateReportLanguageByEmailOut)
def update_report_language_by_email(
    body: UpdateReportLanguageByEmailBody,
    email: str | None = None,
    session: Session = Depends(get_session),
) -> UpdateReportLanguageByEmailOut:
    """Set one user's report language by email (issue #308).

    Grouped with DELETE /users/by-email (issue #274/PR #275) rather than
    next to the by-id POST /users/{user_id}/cadence — group by URL shape
    (by-email vs. by-id), not by "both are user-setting mutations".

    Unlike the by-email purge route, this carries NO re-typed-email
    confirmation ceremony: that pattern exists specifically for an
    irreversible destructive action. Setting a report language is a
    single-field, reversible write with no data-loss risk, so this follows
    the lighter-weight shape of POST /users/{user_id}/cadence instead —
    ops token auth, no confirm param.

    `email` is exact-match only (normalized via `_normalize_email`,
    strip+lowercase, same as the other by-email admin routes) — no partial/
    fuzzy match, since this is a plain local `users` table query, not a
    third-party lookup with its own semantics to second-guess (issue #275's
    vendor-API lesson doesn't apply here).
    """
    normalized_email = _normalize_email(email)
    if normalized_email is None:
        raise HTTPException(status_code=422, detail="email query param is required")
    user = session.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.locale = body.report_language
    session.commit()
    return UpdateReportLanguageByEmailOut(
        user_id=user.id, email=user.email, report_language=user.locale
    )


class UpdateReportCurrencyByEmailBody(BaseModel):
    # Not a Literal — same reasoning as me.py's UpdateReportCurrencyBody:
    # VALID_CURRENCIES (15 members) is checked directly rather than hand-
    # kept as a second/third drifting whitelist.
    report_currency: str

    @field_validator("report_currency")
    @classmethod
    def _validate_report_currency(cls, v: str) -> str:
        if v not in VALID_CURRENCIES:
            raise ValueError(f"unrecognized currency {v!r} — not in VALID_CURRENCIES")
        return v


class UpdateReportCurrencyByEmailOut(BaseModel):
    user_id: UUID
    email: str
    report_currency: str


@router.post("/users/by-email/report-currency", response_model=UpdateReportCurrencyByEmailOut)
def update_report_currency_by_email(
    body: UpdateReportCurrencyByEmailBody,
    email: str | None = None,
    session: Session = Depends(get_session),
) -> UpdateReportCurrencyByEmailOut:
    """Set one user's report/base currency by email (issue #350 item 1) —
    the report-currency sibling of update_report_language_by_email above,
    same grouping (by-email URL shape) and same lighter-weight shape (no
    re-typed-email confirmation ceremony — a single-field, reversible
    write with no data-loss risk).

    A real change also appends `report_currency_changes` with source=admin
    (issue #372). Historical snapshot `base_currency` is not rewritten.
    """
    normalized_email = _normalize_email(email)
    if normalized_email is None:
        raise HTTPException(status_code=422, detail="email query param is required")
    user = session.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    ops_id = UUID(get_settings().ADMIN_ID)
    apply_report_currency_change(
        session,
        user,
        body.report_currency,
        source="admin",
        actor_user_id=ops_id if session.get(User, ops_id) is not None else None,
    )
    session.commit()
    return UpdateReportCurrencyByEmailOut(
        user_id=user.id, email=user.email, report_currency=user.base_currency
    )


class ReportCurrencyChangeOut(BaseModel):
    id: UUID
    user_id: UUID
    old_currency: str
    new_currency: str
    changed_at: datetime
    source: str
    actor_user_id: UUID | None


@router.get(
    "/users/{user_id}/report-currency-audit",
    response_model=list[ReportCurrencyChangeOut],
)
def list_report_currency_audit(
    user_id: UUID,
    session: Session = Depends(get_session),
) -> list[ReportCurrencyChange]:
    """Ops read of one user's report-currency change log (issue #372).

    Newest first. Product UI is out of scope; this is the documented
    admin read path so operators do not need SSH+psql to check whether
    a mid-life preference change explains snapshot `base_currency`
    mismatch. Does not rewrite snapshots.
    """
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return list(
        session.scalars(
            select(ReportCurrencyChange)
            .where(ReportCurrencyChange.user_id == user_id)
            .order_by(ReportCurrencyChange.changed_at.desc(), ReportCurrencyChange.id.desc())
        ).all()
    )


@router.delete("/users/{user_id}", response_model=PurgeUserOut)
def purge_user_endpoint(
    user_id: UUID,
    session: Session = Depends(get_session),
    confirm: str | None = None,
) -> PurgeUserOut:
    """Hard-purge one user's own data (issue #199; Supabase Auth purge and
    the orphan-only path added by issue #225).

    Auth deletion is sequenced strictly before any local delete: Postgres
    and Supabase Auth are two separate systems with no shared transaction,
    so a failure on either side must never leave the other newly orphaned.
    If `delete_auth_user` fails for any reason other than 404 (already
    gone), the request 502s with nothing local touched — retry is always
    safe. Also handles the reverse gap this endpoint used to have no answer
    for: a Supabase Auth account with no matching local row at all.
    """
    user = session.get(User, user_id)
    if user is None:
        # A PK miss on `users.id` is not proof there's no local user for
        # this account: `user_id` could be someone's `auth_subject` passed
        # by mistake (Auth ids and our own PK are both UUIDs, easy to mix
        # up — B4 is explicit they're never the same value). Falling
        # through to the orphan path in that case would Auth-delete a
        # live account, including the seed user's, while its local row
        # sits untouched — the reverse of the orphan this endpoint exists
        # to clean up (review, PR #246 round 2). Check before any Auth
        # call, not after.
        live_owner_id = session.execute(
            select(User.id).where(User.auth_subject == str(user_id))
        ).scalar_one_or_none()
        if live_owner_id is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{user_id} is a Supabase Auth subject already bound to a local "
                    f"user; use DELETE /admin/users/{live_owner_id} instead"
                ),
            )
        return _purge_orphan_auth_user(user_id, confirm)

    return _purge_local_user(session, user, confirm)


class CreateEmailVerificationBody(BaseModel):
    email: str
    purpose: Literal["account_email", "delivery_email", "ops_manual"] = "ops_manual"
    user_id: UUID | None = None

    @field_validator("email")
    @classmethod
    def _email_not_blank(cls, v: str) -> str:
        # Boundary validation (review, PR #261) — a blank/whitespace-only
        # address previously persisted a pending row that could never be
        # confirmed by anyone. Normalization (strip/lower) itself stays in
        # create_verification via _normalize_email, the single place every
        # caller (this endpoint, and any future one) goes through — this
        # check only rejects the one input that has no valid normalized
        # form at all.
        if not v.strip():
            raise ValueError("email must not be blank")
        return v

    @model_validator(mode="after")
    def _purpose_user_id_pairing(self) -> CreateEmailVerificationBody:
        """Design §3.5: an unbound probe is purpose=ops_manual with no
        user_id; a bound call passes the user's real purpose. Any other
        pairing (review, PR #261) silently no-ops on confirm instead of
        failing loudly — ops_manual + user_id skips the write-back
        (_target_field returns None for ops_manual regardless of user_id),
        and account_email/delivery_email with no user_id has no row to load
        a user from. Reject both at the boundary instead of persisting a
        pending row that can never do anything useful."""
        bound = self.purpose in ("account_email", "delivery_email")
        if bound and self.user_id is None:
            raise ValueError(f"purpose={self.purpose} requires user_id")
        if not bound and self.user_id is not None:
            raise ValueError("purpose=ops_manual must not be paired with user_id")
        return self


class EmailVerificationCreateOut(BaseModel):
    id: UUID
    status: str
    expires_at: datetime


class EmailVerificationDetailOut(BaseModel):
    id: UUID
    status: str
    expires_at: datetime
    # Diagnostic fields (review, PR #261) — the stated purpose of this
    # endpoint is post-hoc "why didn't this user get their email" lookup,
    # which `id`/`status`/`expires_at` alone can't answer. Deliberately NOT
    # on EmailVerificationCreateOut above: POST's response stays the narrow
    # create-ack shape (never the plaintext token either way).
    email: str
    purpose: str
    user_id: UUID | None
    provider_message_id: str | None
    last_sent_at: datetime
    verified_at: datetime | None


@router.post("/email-verifications", response_model=EmailVerificationCreateOut, status_code=201)
def create_email_verification_endpoint(
    body: CreateEmailVerificationBody, session: Session = Depends(get_session)
) -> EmailVerificationCreateOut:
    """Trigger a verification for any email address (issue #260, Ring
    1-Email Validation design doc §3.5). Not tied to `users` existing:
    `purpose=ops_manual` (default) with no `user_id` is a pure reachability
    probe. Passing `user_id` + `account_email`/`delivery_email` behaves
    exactly like the corresponding application-scenario trigger — on
    confirm, `delivery_email` is written back to that user's row (and
    `account_email` marks the address already on the row as verified,
    never overwriting it) — those call sites don't exist yet (out of
    scope, see the issue), so this is currently the only way to drive that
    path end-to-end.

    Never returns the plaintext token (§3.2's hash-only discipline — this
    endpoint is not a backdoor around it).
    """
    if body.user_id is not None:
        user = session.get(User, body.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
    try:
        record = create_verification(
            session,
            email=body.email,
            purpose=body.purpose,
            user_id=body.user_id,
        )
    except ResendTooSoon:
        raise HTTPException(
            status_code=429,
            # Scope-accurate wording (round-4 review): for a bound call the
            # cooldown scope is (user_id, purpose), not the request's address
            # — a prior send to a DIFFERENT address for the same user+purpose
            # also trips this. Only the unbound ops_manual probe case is
            # scoped by address.
            detail="a verification for this scope (user+purpose, or address for an "
            "unbound probe) was already sent less than 60s ago",
        ) from None
    except VerificationSendFailed:
        # Nothing local was touched (create_verification sends before it
        # writes anything — review, PR #261 round 2) — safe to retry.
        raise HTTPException(
            status_code=502,
            detail="failed to send the verification email; no local data was touched, retry",
        ) from None
    return EmailVerificationCreateOut(
        id=record.id, status=record.status, expires_at=record.expires_at
    )


@router.get("/email-verifications/{verification_id}", response_model=EmailVerificationDetailOut)
def get_email_verification_endpoint(
    verification_id: UUID, session: Session = Depends(get_session)
) -> EmailVerificationDetailOut:
    """Status lookup (issue #260) — Ops API triggers don't wait synchronously
    for a user to click, so this is the only way to check what happened
    afterward. Widened past just id/status/expires_at (review, PR #261):
    the point of this endpoint is diagnosing "why didn't this user get
    their email" without a database query. No list/filter endpoint yet —
    no real management task has asked for one (Ops API Reference's
    principle: a real management task must exist before the endpoint for
    it does)."""
    record = session.get(EmailVerification, verification_id)
    if record is None:
        raise HTTPException(status_code=404, detail="verification not found")
    return EmailVerificationDetailOut(
        id=record.id,
        status=record.status,
        expires_at=record.expires_at,
        email=record.email,
        purpose=record.purpose,
        user_id=record.user_id,
        provider_message_id=record.provider_message_id,
        last_sent_at=record.last_sent_at,
        verified_at=record.verified_at,
    )


class LeverageOverrideOut(BaseModel):
    ticker: str
    leverage_multiple: Decimal
    direction: Literal["bull", "bear"] | None
    notes: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def from_override(o: LeverageOverride) -> LeverageOverrideOut:
        return LeverageOverrideOut(
            ticker=o.ticker,
            leverage_multiple=o.leverage_multiple,
            direction=o.direction,
            notes=o.notes,
            created_by=o.created_by,
            created_at=o.created_at,
            updated_at=o.updated_at,
        )


class CreateLeverageOverrideBody(BaseModel):
    ticker: str = Field(min_length=1)
    leverage_multiple: Decimal = Field(gt=0)
    direction: Literal["bull", "bear"] | None = None
    notes: str | None = None


@router.post("/ticker-leverage", response_model=LeverageOverrideOut, status_code=201)
def create_ticker_leverage_endpoint(
    body: CreateLeverageOverrideBody, session: Session = Depends(get_session)
) -> LeverageOverrideOut:
    """Issue #87: system-wide leveraged-product multiplier, applied at read
    time by window_data.py (anomaly thresholds widened) and
    portfolio_calculator.py (§4.1 single-holding concentration thresholds
    tightened). Never written back onto Holding.asset_class."""
    try:
        created = create_leverage_override(
            session,
            ticker=body.ticker,
            leverage_multiple=body.leverage_multiple,
            direction=body.direction,
            notes=body.notes,
            created_by=UUID(get_settings().ADMIN_ID),
        )
    except LeverageOverrideAlreadyExists:
        raise HTTPException(
            status_code=409, detail="ticker already has a leverage override"
        ) from None
    session.commit()
    return LeverageOverrideOut.from_override(created)


@router.get("/ticker-leverage", response_model=list[LeverageOverrideOut])
def list_ticker_leverage_endpoint(
    session: Session = Depends(get_session),
) -> list[LeverageOverrideOut]:
    return [LeverageOverrideOut.from_override(o) for o in list_leverage_overrides(session)]


@router.get("/ticker-leverage/{ticker}", response_model=LeverageOverrideOut)
def get_ticker_leverage_endpoint(
    ticker: str, session: Session = Depends(get_session)
) -> LeverageOverrideOut:
    override = get_leverage_override(session, ticker)
    if override is None:
        raise HTTPException(status_code=404, detail="ticker leverage override not found")
    return LeverageOverrideOut.from_override(override)


class UpdateLeverageOverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leverage_multiple: Decimal | None = Field(default=None, gt=0)
    direction: Literal["bull", "bear"] | None = Field(default=None)
    notes: str | None = Field(default=None)


@router.patch("/ticker-leverage/{ticker}", response_model=LeverageOverrideOut)
def update_ticker_leverage_endpoint(
    ticker: str, body: UpdateLeverageOverrideBody, session: Session = Depends(get_session)
) -> LeverageOverrideOut:
    updates = body.model_dump(exclude_unset=True)
    try:
        updated = update_leverage_override(session, ticker, **updates)
    except LookupError:
        raise HTTPException(status_code=404, detail="ticker leverage override not found") from None
    session.commit()
    return LeverageOverrideOut.from_override(updated)


@router.delete("/ticker-leverage/{ticker}", status_code=204)
def delete_ticker_leverage_endpoint(ticker: str, session: Session = Depends(get_session)) -> None:
    try:
        delete_leverage_override(session, ticker)
    except LookupError:
        raise HTTPException(status_code=404, detail="ticker leverage override not found") from None
    session.commit()


@router.get("/users/{user_id}/api-audit", response_model=ApiAuditPage)
def read_api_audit(
    user_id: UUID, start: date, end: date, session: Session = Depends(get_session)
) -> ApiAuditPage:
    if session.get(User, user_id) is None:
        raise HTTPException(404, "user not found")
    if start > end or (end - start).days > 30:
        raise HTTPException(422, "range must be ordered and not exceed 31 days")
    beginning = datetime.combine(start, datetime.min.time(), tzinfo=ET)
    ending = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=ET)
    rows = session.scalars(
        select(ApiAuditLog)
        .where(
            ApiAuditLog.user_id == user_id,
            ApiAuditLog.occurred_at >= beginning,
            ApiAuditLog.occurred_at < ending,
        )
        .order_by(ApiAuditLog.occurred_at.desc(), ApiAuditLog.id.desc())
        .limit(1001)
    ).all()
    return ApiAuditPage(
        rows=[ApiAuditOut.model_validate(row) for row in rows[:1000]], truncated=len(rows) > 1000
    )


@router.post("/users/{user_id}/api-tokens/revoke-all", response_model=RevokedTokensOut)
def revoke_api_tokens(user_id: UUID, session: Session = Depends(get_session)) -> RevokedTokensOut:
    if session.get(User, user_id) is None:
        raise HTTPException(404, "user not found")
    count = revoke_all_api_tokens(session, user_id, "ops")
    session.commit()
    return RevokedTokensOut(revoked_count=count)
