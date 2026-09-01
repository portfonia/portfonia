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
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import openai
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.deps import require_ops_token
from app.core.ops_log import log_ops_event
from app.core.rate_limit import rate_limit_create_invite
from app.models.email_verification import EmailVerification
from app.models.holding import Holding
from app.models.invite import Invite
from app.models.report import Report
from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext
from app.schemas.reports import ReportOut
from app.services import fx_fetcher, price_fetcher
from app.services.auth_provider import (
    AuthProviderError,
    AuthUserInfo,
    delete_auth_user,
    get_auth_user,
    get_auth_user_by_email,
)
from app.services.email_verification import (
    ResendTooSoon,
    VerificationSendFailed,
    create_verification,
)
from app.services.fund_nav_fetcher import update_fund_navs
from app.services.invites import (
    EmailAlreadyRegistered,
    _normalize_email,
    create_invite,
    list_invites,
    revoke_invite,
)
from app.services.llm_errors import LLMEmptyResponseError
from app.services.report_generator import generate_report
from app.services.user_purge import purge_user
from app.tasks.admin_tasks import send_admin_alert_task

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


class RefreshResult(BaseModel):
    prices_updated: int
    prices_failed: list[str]
    sectors_backfilled: int
    funds_updated: int
    funds_failed: list[str]
    fx_upserted: int
    fx_failed: list[str]


@router.post("/portfolio/refresh", response_model=RefreshResult)
def refresh_market_data(session: Session = Depends(get_session)) -> RefreshResult:
    """Manually trigger price / sector / NAV / FX refresh.

    Moved from POST /portfolio/refresh (decision point 8/11): this pulls
    fresh market data for every user's holdings at once, so it's an ops
    action, not something an individual user should be able to trigger.
    """
    prices = price_fetcher.update_holding_prices(session)
    sectors = price_fetcher.backfill_sectors(session)
    funds = update_fund_navs(session)
    fx = fx_fetcher.update_fx_rates(session)
    session.commit()
    return RefreshResult(
        prices_updated=prices.updated,
        prices_failed=prices.failed,
        sectors_backfilled=sectors,
        funds_updated=funds.updated,
        funds_failed=funds.failed,
        fx_upserted=fx.upserted,
        fx_failed=fx.failed,
    )


class CreateInviteBody(BaseModel):
    email: str | None = None
    expires_days: int = Field(default=14, ge=1, le=90)


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
            created_by=UUID(get_settings().DEV_USER_ID),
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
    report_cadence: Literal["mwf", "weekly"]


class UpdateCadenceOut(BaseModel):
    id: UUID
    email: str
    report_cadence: str


@router.post("/users/{user_id}/cadence", response_model=UpdateCadenceOut)
def update_user_cadence(
    user_id: UUID, body: UpdateCadenceBody, session: Session = Depends(get_session)
) -> UpdateCadenceOut:
    """Change a user's report_cadence (issue #191).

    Intended to be reusable later for self-service cadence selection
    (post-auth, post-billing) — that reuse is out of scope here, this ships
    the endpoint's read/write/validation logic only.
    """
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.report_cadence = body.report_cadence
    session.commit()
    return UpdateCadenceOut(id=user.id, email=user.email, report_cadence=user.report_cadence)


# Literal members must be compile-time, so these are hand-kept copies of
# app.models.user.VALID_USER_STATUSES / VALID_REPORT_CADENCES — same
# discipline as UpdateCadenceBody (PR #248); a drift test over both copies
# lives in test_admin_users.py.
UserStatusFilter = Literal["active", "deleted", "suspended"]
ReportCadenceFilter = Literal["mwf", "weekly"]


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
        )
        for user, holdings_count_value, has_context_count in rows
    ]


@router.post(
    "/users/{user_id}/reports/generate",
    response_model=ReportOut,
    status_code=201,
)
def generate_report_for_user(user_id: UUID, session: Session = Depends(get_session)) -> Report:
    """Manually trigger report generation for one user (issue #201).

    Synchronous by design: /admin/* hits api.portfonia.com directly, never
    the frontend Next.js proxy that times out around 30s (issue #193). A
    curl or agent caller waits for the full pipeline. The handler is a
    sync def, so Starlette runs it in the threadpool — it occupies a
    threadpool worker and a pooled DB connection for the full duration,
    the same cost POST /reports/generate already pays. Acceptable because
    this is a rare ops action. Do not invoke concurrently for the same
    user; a second POST while the first is still running can race the
    report dedup key.

    session_node is always "manual", matching the self-service default, so
    a same-day scheduled after_close run still gets its own row. Currency
    and language are the system-wide defaults (generate_report's USD,
    Settings.OUTPUT_LANG), not users.base_currency / users.locale — same
    as the scheduled fan-out and the self-service endpoint. The user must
    exist and be status=active. No holdings precondition (issue #221 §2.7):
    an empty book renders §1/distribution/§4.1/§4.2/§4.4 as empty tables
    rather than failing — self-service POST /reports/generate never had
    this check either. active_user_ids() (the scheduled fan-out, issue #191)
    still requires a holding row for the mwf cadence; weekly does not, so a
    weekly user's manual-generate behavior here already matches what
    scheduled fan-out will eventually do for them, not a relaxation unique
    to this endpoint.

    A successful run generates the report but does not always email it:
    this handler is exempt from the Layer 1 generation gate (issue #276 —
    it resolves the user directly, never `active_user_ids()`), so an
    active but unverified user still gets a Report row, while the Layer 2
    send-time gate then skips delivery — `email_sent_at` stays null and
    the no-verified-recipient ops alert fires. needs_review does not
    email. Quiet-day heartbeats email unless the short-manual-window
    suppression applies. A repeat same-day call on an already-complete
    report is an idempotent no-op (still 201, matching POST
    /reports/generate) and does not re-send.
    """
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.status != "active":
        raise HTTPException(status_code=422, detail="user is not active")
    try:
        return generate_report(
            session,
            user_id=user_id,
            output_lang=get_settings().OUTPUT_LANG,
            session_node="manual",
        )
    except LLMEmptyResponseError as exc:
        raise HTTPException(
            status_code=502, detail=f"LLM returned an empty response: {exc}"
        ) from exc
    except openai.APIError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="report generation already in progress"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Report generation failed: {exc}") from exc


class PurgeDeletedCounts(BaseModel):
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


_NO_LOCAL_ROWS = PurgeDeletedCounts(
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
    if user.id == UUID(get_settings().DEV_USER_ID):
        raise HTTPException(status_code=409, detail="refusing to delete the seed user")
    created_invites = session.execute(select(exists().where(Invite.created_by == user.id))).scalar()
    if created_invites:
        raise HTTPException(
            status_code=409, detail="user created invites; revoke or reassign first"
        )
    if confirm is None:
        raise HTTPException(status_code=422, detail="confirm query param is required")
    if _normalize_email(confirm) != _normalize_email(user.email):
        raise HTTPException(status_code=409, detail="confirm does not match user email")

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
