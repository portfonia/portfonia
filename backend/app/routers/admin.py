"""Ops API channel (issue #128 Ring 1 stage B, checkpoint B2).

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

import time
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.core.deps import require_ops_token
from app.core.ops_log import log_ops_event
from app.services import fx_fetcher, price_fetcher
from app.services.email_sender import send_ops_alert
from app.services.fund_nav_fetcher import update_fund_navs

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
                        send_ops_alert(
                            "Portfonia ops: repeated /admin unauthorized attempts",
                            f"{_consecutive_401_count} consecutive unauthorized /admin/* "
                            f"requests, most recently {request.method} {request.url.path}. "
                            "No legitimate caller should ever guess wrong this many times "
                            "in a row.",
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
