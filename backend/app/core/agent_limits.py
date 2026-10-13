"""Agent-only quiet windows and per-user fixed-window request limits."""

import copy
import math
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from app.core.rate_limit import (
    RATE_LIMIT_DETAIL,
    UNAVAILABLE_DETAIL,
    RateLimitUnavailable,
    get_backend,
)
from app.core.timezones import ET
from app.tasks import API_QUIET_BEAT_ENTRIES, celery_app


def quiet_retry_after(now: datetime) -> int | None:
    store = get_backend()
    if store.any_key("agent:quiet:active:"):
        return 300
    cooldown = store.ttl("agent:quiet:cooldown")
    if cooldown > 0:
        return cooldown
    moment = now.astimezone(UTC)
    # Celery searches strictly after last_run_at; include the lower boundary.
    anchor = (moment - timedelta(seconds=300, microseconds=1)).astimezone(ET)
    for name, quiet in API_QUIET_BEAT_ENTRIES.items():
        if not quiet:
            continue
        cron = copy.copy(celery_app.conf.beat_schedule[name]["schedule"])
        cron.nowfun = lambda pinned=anchor: pinned
        fire: datetime = anchor.astimezone(UTC) + cron.remaining_estimate(anchor)
        if fire <= moment + timedelta(seconds=300):
            return max(1, math.ceil((fire + timedelta(seconds=300) - moment).total_seconds()))
    return None


def _refuse(seconds: int) -> None:
    raise HTTPException(429, RATE_LIMIT_DETAIL, headers={"Retry-After": str(max(1, seconds))})


# Future single-row holdings writes. Whole-book GET/PUT and validate stay on
# the older burst and per-endpoint hour counters.
_SINGLE_ROW_WRITES = {
    ("POST", "/agent/v1/holdings"),
    ("PUT", "/agent/v1/holdings/{holding_id}"),
    ("PATCH", "/agent/v1/holdings/{holding_id}"),
    ("DELETE", "/agent/v1/holdings/{holding_id}"),
}


def enforce_agent_limits(user_id: str, method: str, route: str, now: datetime) -> None:
    store = get_backend()
    try:
        quiet = quiet_retry_after(now)
        if quiet is not None:
            _refuse(quiet)
        locked = store.ttl(f"agent:lock:{user_id}")
        if locked > 0:
            _refuse(locked)
        if (method, route) in _SINGLE_ROW_WRITES:
            if store.incr_with_ttl(f"agent:single:minute:{user_id}", 60) > 30:
                store.set_ttl(f"agent:lock:{user_id}", 900)
                _refuse(900)
            hour = f"agent:single:hour:{user_id}"
            if store.incr_with_ttl(hour, 3600) > 100:
                _refuse(store.ttl(hour))
            return
        if store.incr_with_ttl(f"agent:burst:{user_id}", 60) > 10:
            store.set_ttl(f"agent:lock:{user_id}", 900)
            _refuse(900)
        hour = f"agent:hour:{route}:{user_id}"
        if store.incr_with_ttl(hour, 3600) > 20:
            _refuse(store.ttl(hour))
    except RateLimitUnavailable:
        raise HTTPException(503, UNAVAILABLE_DETAIL) from None
