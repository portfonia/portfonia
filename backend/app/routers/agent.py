"""Personal reports, snapshots, intelligence and strict current holdings API."""

import logging
import re
from datetime import date
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.core.agent_auth import AgentPrincipal, agent_principal
from app.core.agent_limits import enforce_agent_limits
from app.core.database import get_session
from app.core.rate_limit import UNAVAILABLE_DETAIL, RateLimitUnavailable, get_backend
from app.schemas.agent import AgentIntelOut, AgentReportsOut
from app.schemas.agent_holdings import Create, Patch, ReplaceAll, Validation
from app.schemas.portfolio import SnapshotExportOut
from app.services import agent_holdings, api_tokens
from app.services.agent_pull import pull_intel, pull_reports
from app.services.portfolio_snapshot_export import export_snapshot_range
from app.services.subscription import is_advanced
from app.tasks.notification_tasks import send_api_access_notice_task


def agent_limits(request: Request, principal: AgentPrincipal = Depends(agent_principal)) -> None:
    enforce_agent_limits(
        str(principal.user_id),
        request.method,
        request.scope["route"].path,
        api_tokens.now_et(),
    )


def advanced_access(
    principal: AgentPrincipal = Depends(agent_principal), _limits: None = Depends(agent_limits)
) -> None:
    if not is_advanced(principal.user):
        raise HTTPException(403, "subscription_required")


router = APIRouter(dependencies=[Depends(agent_limits)])


@router.get(
    "/snapshots",
    response_model=SnapshotExportOut,
    dependencies=[Depends(advanced_access)],
    summary="Read your complete daily holding snapshots (Advanced)",
)
def snapshots(
    request: Request,
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> SnapshotExportOut:
    result = export_snapshot_range(session, principal.user_id, start, end)
    now = api_tokens.now_et()
    try:
        claimed = get_backend().set_nx(
            f"agent:notice:{principal.user_id}:{now.date().isoformat()}", 129600
        )
    except RateLimitUnavailable:
        raise HTTPException(503, UNAVAILABLE_DETAIL) from None
    if claimed:
        send_api_access_notice_task.delay(str(principal.user_id))
    request.state.agent_item_count = len(result.days)
    return result


@router.get(
    "/reports",
    response_model=AgentReportsOut,
    summary="Read your five newest available reports in a date range",
)
def reports(
    request: Request,
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> AgentReportsOut:
    result = pull_reports(session, principal.user_id, start, end)
    request.state.agent_item_count = len(result.items)
    return result


@router.get(
    "/intel",
    response_model=AgentIntelOut,
    dependencies=[Depends(advanced_access)],
    summary="Read your holding and macro intelligence for one ET date (Advanced)",
)
def intel(
    request: Request,
    date: Annotated[date, Query()],
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> AgentIntelOut:
    result = pull_intel(session, principal.user_id, date)
    request.state.agent_item_count = sum(
        len(h.headlines) + len(h.articles) for h in result.holdings
    ) + (sum(len(t.articles) for t in result.macro.themes) if result.macro else 0)
    return result


# Current holdings use structured domain errors while existing read routes
# retain their original payloads.


def holdings_access(
    principal: AgentPrincipal = Depends(agent_principal), _limits: None = Depends(agent_limits)
) -> None:
    agent_holdings.entitlement(principal.user)


def holdings_write_access(
    principal: AgentPrincipal = Depends(agent_principal), _access: None = Depends(holdings_access)
) -> None:
    if principal.permission != "holdings_write":
        raise agent_holdings.error(
            403, "holdings_write_required", "Use a token with holdings_write permission."
        )


def _data_notice(uid: UUID, *, modified: bool) -> None:
    from app.tasks.notification_tasks import send_api_modification_notice_task

    marker = "modification" if modified else "notice"
    try:
        claimed = get_backend().set_nx(
            f"agent:{marker}:{uid}:{api_tokens.now_et().date().isoformat()}", 129600
        )
        if claimed:
            task = send_api_modification_notice_task if modified else send_api_access_notice_task
            task.delay(str(uid))
    except Exception:
        # This function also runs after durable commit: never expose payload,
        # credentials, keys, recipient addresses or a misleading failed write.
        logging.getLogger(__name__).error(
            "Agent data notice enqueue failed user_id=%s kind=%s", uid, marker
        )


@router.get("/holdings", dependencies=[Depends(holdings_access)])
def current_holdings(
    request: Request,
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> Response:
    result = agent_holdings.get_book(session, principal.user_id)
    request.state.agent_item_count = len(cast(list[object], result["holdings"]))
    _data_notice(principal.user_id, modified=False)
    return JSONResponse(result)


async def create_body(request: Request) -> Create:
    return await parse_holdings_body(request, TypeAdapter(Create))


async def replace_all_body(request: Request) -> ReplaceAll:
    return await parse_holdings_body(request, TypeAdapter(ReplaceAll))


async def patch_body(request: Request) -> Patch:
    return await parse_holdings_body(request, TypeAdapter(Patch))


async def validation_body(request: Request) -> Validation:
    return await parse_holdings_body(request, TypeAdapter[Validation](Validation))


async def parse_holdings_body[T](request: Request, schema: TypeAdapter[T]) -> T:
    # JSON parsing is deliberately a dependency after auth/limits/entitlement;
    # FastAPI's eager Body parsing otherwise rejects malformed JSON first.
    try:
        return schema.validate_json(await request.body())
    except ValidationError as exc:
        raise RequestValidationError(
            [{**entry, "loc": ("body", *entry["loc"])} for entry in exc.errors(include_url=False)]
        ) from None


@router.post("/holdings/validate", dependencies=[Depends(holdings_access)])
def validate_holdings(
    request: Request,
    body: Validation = Depends(validation_body),
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> Response:
    request.state.agent_item_count = len(body.holdings) if isinstance(body, ReplaceAll) else 1
    return JSONResponse(agent_holdings.validate(session, principal.user_id, body))


def _write_result(
    request: Request,
    principal: AgentPrincipal,
    session: Session,
    body: Create | ReplaceAll | Patch | None,
    *,
    hid: UUID | None = None,
    version: str | None = None,
) -> Response:
    if body is not None and "if-match" in request.headers:
        raise agent_holdings.error(422, "invalid_holding", "Supply only the body version.")
    status, payload, replayed, changed = agent_holdings.mutate(
        session,
        principal.user_id,
        principal.permission,
        body,
        hid=hid,
        version=version,
        key=request.headers.get("idempotency-key"),
    )
    request.state.agent_item_count = (
        len(cast(list[object], payload["holdings"])) if isinstance(body, ReplaceAll) else 1
    )
    if changed:
        _data_notice(principal.user_id, modified=True)
    return JSONResponse(
        payload, status_code=status, headers={"Idempotency-Replayed": "true"} if replayed else None
    )


@router.post("/holdings", dependencies=[Depends(holdings_write_access)])
def create_current_holding(
    request: Request,
    body: Create = Depends(create_body),
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> Response:
    return _write_result(request, principal, session, body)


@router.put("/holdings", dependencies=[Depends(holdings_write_access)])
def replace_current_holdings(
    request: Request,
    body: ReplaceAll = Depends(replace_all_body),
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> Response:
    return _write_result(request, principal, session, body)


@router.put("/holdings/{holding_id}", dependencies=[Depends(holdings_write_access)])
def replace_current_holding(
    request: Request,
    holding_id: UUID,
    body: Create = Depends(create_body),
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> Response:
    return _write_result(request, principal, session, body, hid=holding_id)


@router.patch("/holdings/{holding_id}", dependencies=[Depends(holdings_write_access)])
def patch_current_holding(
    request: Request,
    holding_id: UUID,
    body: Patch = Depends(patch_body),
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> Response:
    return _write_result(request, principal, session, body, hid=holding_id)


@router.delete("/holdings/{holding_id}", dependencies=[Depends(holdings_write_access)])
async def delete_current_holding(
    request: Request,
    holding_id: UUID,
    principal: AgentPrincipal = Depends(agent_principal),
    session: Session = Depends(get_session),
) -> Response:
    if await request.body():
        raise agent_holdings.error(422, "invalid_holding", "DELETE accepts no JSON body.")
    header = request.headers.get("if-match")
    if header is None:
        raise agent_holdings.error(
            428, "version_required", "Supply the quoted current version in If-Match."
        )
    if re.fullmatch(r'"(0|[1-9][0-9]*)"', header) is None:
        raise agent_holdings.error(
            422, "invalid_holding", "If-Match must be one quoted canonical version."
        )
    return _write_result(request, principal, session, None, hid=holding_id, version=header[1:-1])
