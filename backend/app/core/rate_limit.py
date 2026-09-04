"""Fixed-window rate limits for signup, invite minting (issue #190),
forgot-password (issue #231), and the portfolio overview email's 15-minute
per-user send cooldown (issue #202 — a `set_nx` claim/release, not a
fixed-window counter like the others, since a routine cooldown hit here is
not an abuse signal worth an ops alert)."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
from datetime import UTC, datetime
from typing import Protocol

from fastapi import HTTPException, Request, status
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.invite import Invite
from app.services.invites import hash_invite_token
from app.tasks.admin_tasks import send_admin_alert_task

logger = logging.getLogger(__name__)

RATE_LIMIT_DETAIL = "too many attempts, try again later"
UNAVAILABLE_DETAIL = "temporarily unavailable"

SIGNUP_IP_MINUTE_LIMIT = 5
SIGNUP_IP_MINUTE_TTL = 60
SIGNUP_IP_HOUR_LIMIT = 20
SIGNUP_IP_HOUR_TTL = 3600
SIGNUP_TOKEN_FAIL_LIMIT = 10
SIGNUP_TOKEN_FAIL_TTL = 3600
INVITE_IP_MINUTE_LIMIT = 10
INVITE_IP_MINUTE_TTL = 60
INVITE_IP_HOUR_LIMIT = 30
INVITE_IP_HOUR_TTL = 3600
SIGNUP_GLOBAL_ALERT_LIMIT = 200
SIGNUP_GLOBAL_TTL = 86400
INVITE_GLOBAL_ALERT_LIMIT = 200
INVITE_GLOBAL_TTL = 86400
# Forgot-password (issue #231): same fixed-window shape as signup's IP
# buckets, plus a per-email bucket (signup has no equivalent — an invite
# token is already a scarce, per-attempt credential; an email address here
# is not). Tighter than signup's IP limits because this endpoint also
# guards the one place account existence can be enumerated (design decision
# in the issue: the response deliberately distinguishes found/not-found).
FORGOT_PASSWORD_IP_MINUTE_LIMIT = 5
FORGOT_PASSWORD_IP_MINUTE_TTL = 60
FORGOT_PASSWORD_IP_HOUR_LIMIT = 20
FORGOT_PASSWORD_IP_HOUR_TTL = 3600
FORGOT_PASSWORD_EMAIL_HOUR_LIMIT = 3
FORGOT_PASSWORD_EMAIL_HOUR_TTL = 3600
# Resend email-verification (issue #262, Profile Page.md §8.3): two buckets
# in front of POST /email-verifications/{id}/resend — a per-user bucket
# (same magnitude as forgot-password's email limit) and a per-address GLOBAL
# bucket deliberately not scoped by user, so several accounts aimed at one
# victim's address share a single allowance (Email Validation.md §3.4's
# mail-bomb scenario). forgot-password's email bucket doesn't need this
# split because its own email bucket already is the per-address one; here
# the authenticated per-user dimension and the per-address dimension are
# two different abuse surfaces.
RESEND_VERIFICATION_USER_HOUR_LIMIT = 3
RESEND_VERIFICATION_USER_HOUR_TTL = 3600
RESEND_VERIFICATION_EMAIL_HOUR_LIMIT = 3
RESEND_VERIFICATION_EMAIL_HOUR_TTL = 3600

_INCR_EXPIRE = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return n
"""


class RateLimitUnavailable(Exception):
    """Counter store failed; callers map this to HTTP 503."""


class CounterBackend(Protocol):
    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int: ...
    def ttl(self, key: str) -> int: ...
    def set_nx(self, key: str, ttl_seconds: int) -> bool: ...
    def delete(self, key: str) -> None: ...


class InMemoryBackend:
    """Same INCR-then-EXPIRE-on-first-hit semantics as the Redis Lua script."""

    def __init__(self) -> None:
        self._now = 0.0
        self._data: dict[str, tuple[int, float]] = {}

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def stored_keys(self) -> list[str]:
        self._purge()
        return list(self._data)

    def _purge(self) -> None:
        expired = [key for key, (_, exp) in self._data.items() if exp <= self._now]
        for key in expired:
            del self._data[key]

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        self._purge()
        if key not in self._data:
            self._data[key] = (1, self._now + ttl_seconds)
            return 1
        count, exp = self._data[key]
        count += 1
        self._data[key] = (count, exp)
        return count

    def ttl(self, key: str) -> int:
        self._purge()
        if key not in self._data:
            return -2
        _, exp = self._data[key]
        remaining = int(exp - self._now)
        return remaining if remaining > 0 else -2

    def set_nx(self, key: str, ttl_seconds: int) -> bool:
        self._purge()
        if key in self._data:
            return False
        self._data[key] = (1, self._now + ttl_seconds)
        return True

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class RedisBackend:
    def __init__(self, client: Redis) -> None:
        self._client = client
        self._incr = client.register_script(_INCR_EXPIRE)

    @classmethod
    def from_settings(cls) -> RedisBackend:
        return cls(Redis.from_url(get_settings().redis_url, decode_responses=True))

    def incr_with_ttl(self, key: str, ttl_seconds: int) -> int:
        try:
            raw: object = self._incr(keys=[key], args=[int(ttl_seconds)])
        except RedisError as exc:
            raise RateLimitUnavailable from exc
        if not isinstance(raw, int):
            raise RateLimitUnavailable
        return raw

    def ttl(self, key: str) -> int:
        try:
            raw: object = self._client.ttl(key)
        except RedisError as exc:
            raise RateLimitUnavailable from exc
        if not isinstance(raw, int):
            raise RateLimitUnavailable
        return raw

    def set_nx(self, key: str, ttl_seconds: int) -> bool:
        try:
            return bool(self._client.set(key, "1", ex=int(ttl_seconds), nx=True))
        except RedisError as exc:
            raise RateLimitUnavailable from exc

    def delete(self, key: str) -> None:
        try:
            self._client.delete(key)
        except RedisError as exc:
            raise RateLimitUnavailable from exc


_override: CounterBackend | None = None
_redis: RedisBackend | None = None


def set_backend(backend: CounterBackend | None) -> None:
    global _override
    _override = backend


def get_backend() -> CounterBackend:
    global _redis
    if _override is not None:
        return _override
    if _redis is None:
        _redis = RedisBackend.from_settings()
    return _redis


def canonical_client_id(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    mapped = addr.ipv4_mapped if isinstance(addr, ipaddress.IPv6Address) else None
    if mapped is not None:
        addr = mapped
    if isinstance(addr, ipaddress.IPv6Address):
        network = ipaddress.IPv6Network((addr, 64), strict=False)
        return str(network.network_address)
    return str(addr)


def client_id_from_request(request: Request) -> str:
    """Key the limiter on the ASGI peer.

    Production uvicorn runs `--proxy-headers --forwarded-allow-ips=*`, which
    rewrites `request.client` from X-Forwarded-For after the Next.js hop
    forwards Caddy's headers (issue #190). Application code does not parse
    XFF. Tests mount the same ProxyHeadersMiddleware on TestClient so XFF
    keying matches production.
    """
    host = request.client.host if request.client is not None else "unknown"
    return canonical_client_id(host)


def _enqueue_alert(subject: str, body: str) -> None:
    try:
        send_admin_alert_task.delay(subject, body)
    except Exception:
        logger.exception("rate_limit: failed to enqueue ops alert")


def _maybe_alert(*, scope: str, bucket: str, window: int, subject: str, body: str) -> None:
    try:
        first = get_backend().set_nx(f"rl:alert:{scope}:{bucket}:{window}", window)
    except RateLimitUnavailable:
        logger.exception("rate_limit: alert dedup store unavailable")
        return
    if not first:
        return
    _enqueue_alert(subject, body)


def _protecting_incr(key: str, ttl_seconds: int) -> int:
    try:
        return get_backend().incr_with_ttl(key, ttl_seconds)
    except RateLimitUnavailable:
        logger.exception("rate_limit: counter store unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("rate_limit: counter failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL
        ) from None


def _trip(key: str, ttl_seconds: int, *, scope: str, bucket: str) -> None:
    try:
        remaining = get_backend().ttl(key)
    except RateLimitUnavailable:
        remaining = ttl_seconds
    retry_after = remaining if remaining > 0 else ttl_seconds
    _maybe_alert(
        scope=scope,
        bucket=bucket,
        window=ttl_seconds,
        subject=f"Portfonia ops: rate limit tripped ({scope})",
        body=f"scope={scope} bucket={bucket} window={ttl_seconds}s retry_after={retry_after}s",
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=RATE_LIMIT_DETAIL,
        headers={"Retry-After": str(retry_after)},
    )


def _enforce_ip(
    prefix: str,
    client_id: str,
    windows: tuple[tuple[int, int], ...],
    *,
    scope: str,
) -> None:
    for limit, ttl in windows:
        key = f"{prefix}:{client_id}:{ttl}"
        n = _protecting_incr(key, ttl)
        if n > limit:
            _trip(key, ttl, scope=scope, bucket=client_id)


def _note_global_volume(
    *,
    key_prefix: str,
    scope: str,
    subject: str,
    noun: str,
    limit: int,
    ttl: int,
) -> None:
    day = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    key = f"{key_prefix}:{day}"
    try:
        n = get_backend().incr_with_ttl(key, ttl)
    except RateLimitUnavailable:
        logger.exception("rate_limit: global %s counter unavailable", noun)
        return
    except Exception:
        logger.exception("rate_limit: global %s counter failed", noun)
        return
    if n >= limit:
        _maybe_alert(
            scope=scope,
            bucket=day,
            window=ttl,
            subject=subject,
            body=(f"{n} {noun} attempts today (UTC {day}); threshold {limit}. Not auto-blocked."),
        )


def _note_global_signup() -> None:
    _note_global_volume(
        key_prefix="rl:signup:global",
        scope="signup-global",
        subject="Portfonia ops: signup volume circuit",
        noun="signup",
        limit=SIGNUP_GLOBAL_ALERT_LIMIT,
        ttl=SIGNUP_GLOBAL_TTL,
    )


def _note_global_invite_mint() -> None:
    _note_global_volume(
        key_prefix="rl:invites:global",
        scope="invites-global",
        subject="Portfonia ops: invite mint volume circuit",
        noun="invite-mint",
        limit=INVITE_GLOBAL_ALERT_LIMIT,
        ttl=INVITE_GLOBAL_TTL,
    )


def rate_limit_signup(request: Request) -> None:
    client_id = client_id_from_request(request)
    _note_global_signup()
    _enforce_ip(
        "rl:signup:ip",
        client_id,
        (
            (SIGNUP_IP_MINUTE_LIMIT, SIGNUP_IP_MINUTE_TTL),
            (SIGNUP_IP_HOUR_LIMIT, SIGNUP_IP_HOUR_TTL),
        ),
        scope="signup",
    )


def rate_limit_create_invite(request: Request) -> None:
    client_id = client_id_from_request(request)
    _note_global_invite_mint()
    _enforce_ip(
        "rl:invites:ip",
        client_id,
        (
            (INVITE_IP_MINUTE_LIMIT, INVITE_IP_MINUTE_TTL),
            (INVITE_IP_HOUR_LIMIT, INVITE_IP_HOUR_TTL),
        ),
        scope="invites",
    )


def rate_limit_forgot_password(request: Request, email: str) -> None:
    """IP + email fixed-window limits for POST /auth/forgot-password (issue #231).

    Reuses the same `_enforce_ip`/`_protecting_incr` machinery as
    rate_limit_signup/rate_limit_create_invite (issue #190) rather than a
    parallel implementation — fail-closed on Redis down is inherited from
    `_protecting_incr` raising RateLimitUnavailable -> HTTPException(503).
    The email bucket is keyed by a hash, not the raw address, so Redis never
    stores a plaintext email as part of a key name.
    """
    client_id = client_id_from_request(request)
    _enforce_ip(
        "rl:forgot_password:ip",
        client_id,
        (
            (FORGOT_PASSWORD_IP_MINUTE_LIMIT, FORGOT_PASSWORD_IP_MINUTE_TTL),
            (FORGOT_PASSWORD_IP_HOUR_LIMIT, FORGOT_PASSWORD_IP_HOUR_TTL),
        ),
        scope="forgot-password-ip",
    )
    email_bucket = hashlib.sha256(email.encode()).hexdigest()
    _enforce_ip(
        "rl:forgot_password:email",
        email_bucket,
        ((FORGOT_PASSWORD_EMAIL_HOUR_LIMIT, FORGOT_PASSWORD_EMAIL_HOUR_TTL),),
        scope="forgot-password-email",
    )


def guard_known_invite_token(session: Session, token: str) -> None:
    """Count attempts only for tokens that already exist in `invites`.

    Unknown strings must not create Redis keys (scanner key explosion).
    """
    token_hash = hash_invite_token(token)
    found = session.execute(
        select(Invite.id).where(Invite.token_hash == token_hash).limit(1)
    ).first()
    if found is None:
        return
    key = f"rl:signup:token:{token_hash}:{SIGNUP_TOKEN_FAIL_TTL}"
    n = _protecting_incr(key, SIGNUP_TOKEN_FAIL_TTL)
    if n > SIGNUP_TOKEN_FAIL_LIMIT:
        _trip(key, SIGNUP_TOKEN_FAIL_TTL, scope="signup-token", bucket=token_hash[:12])


def rate_limit_enforce_resend_verification(*, user_id: str, email: str) -> None:
    """Fixed-window limiter for POST /email-verifications/{id}/resend
    (issue #262, Profile Page.md §8.3). Two buckets: per-user and a GLOBAL
    per-address bucket keyed by sha256(email) — the raw address never
    becomes part of a Redis key name. Called at the router layer only;
    create_verification's own 60s data-driven cooldown (the Ops-API
    simplification) is unchanged and orthogonal to this. RateLimitUnavailable
    from _protecting_incr propagates as 503 — fail-closed, same as signup/
    forgot-password."""
    _enforce_ip(
        "rl:resend_verification:user",
        user_id,
        ((RESEND_VERIFICATION_USER_HOUR_LIMIT, RESEND_VERIFICATION_USER_HOUR_TTL),),
        scope="resend-verification-user",
    )
    email_bucket = hashlib.sha256(email.encode()).hexdigest()
    _enforce_ip(
        "rl:resend_verification:email",
        email_bucket,
        ((RESEND_VERIFICATION_EMAIL_HOUR_LIMIT, RESEND_VERIFICATION_EMAIL_HOUR_TTL),),
        scope="resend-verification-email",
    )


# issue #202: cooldown for POST /portfolio/send-overview. Deliberately NOT
# built on `_enforce_ip`/`_trip` — those alert on every trip (right for an
# abuse-shaped limiter like resend-verification, where hitting the limit is
# itself the signal), but a user clicking this button twice inside 15
# minutes is the routine, expected case, not an anomaly. This uses `set_nx`
# as an atomic claim instead: at most one concurrent caller wins it, closing
# the double-click race without a separate check-then-set step.
PORTFOLIO_OVERVIEW_COOLDOWN_SECONDS = 900


def _portfolio_overview_key(user_id: str) -> str:
    return f"rl:portfolio_overview:{user_id}"


def check_portfolio_overview_cooldown(user_id: str) -> int | None:
    """Claim the send slot for *user_id*, or report seconds left to wait.

    Returns `None` and claims the cooldown window if the user is clear to
    send. Returns the remaining seconds (>0) if still in cooldown — the
    caller sends nothing in that case. Fails closed (503) if the counter
    store is unavailable, consistent with every other limiter in this
    module.
    """
    key = _portfolio_overview_key(user_id)
    try:
        claimed = get_backend().set_nx(key, PORTFOLIO_OVERVIEW_COOLDOWN_SECONDS)
        if claimed:
            return None
        remaining = get_backend().ttl(key)
    except RateLimitUnavailable:
        logger.exception("rate_limit: portfolio overview cooldown store unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL
        ) from None
    return remaining if remaining > 0 else PORTFOLIO_OVERVIEW_COOLDOWN_SECONDS


def release_portfolio_overview_cooldown(user_id: str) -> None:
    """Undo a claim made by `check_portfolio_overview_cooldown` (review
    5100733033 leftover): if the router's `.delay()` call right after the
    claim actually fails (broker down), the send never happened at all —
    without this, the user would be locked out of retrying for the full
    15 minutes over a message that was never even queued. Best-effort: a
    store outage here just leaves the claim in place (the user waits out
    the cooldown as normal), it must not itself raise and turn a caller's
    already-logged enqueue failure into an unhandled second exception.
    """
    try:
        get_backend().delete(_portfolio_overview_key(user_id))
    except RateLimitUnavailable:
        logger.warning(
            "rate_limit: could not release portfolio overview cooldown for user_id=%s "
            "(store unavailable) — it will expire naturally",
            user_id,
        )


# issue #104 (Ring 1-Email Validation.md, 2026-09-03 section, frozen
# requirement #6): 15-minute cooldown on MANUAL report resends only (admin
# `resend=true`, future self-service regenerate+resend) — NOT the scheduled
# cadence fan-out, which the design explicitly excludes (a user can only
# have one report_cadence at a time, so there's no cross-cadence race for
# this to guard against). Keyed by RECIPIENT ADDRESS, not report id or user
# id: a `Report` row is overwritten in place on resend (unlike
# email_verifications' append-only supersede), so a manual resend racing
# `poll_report_delivery`'s 10-minute (POLL_DELAY_SECONDS) delayed read of
# the same row would make that stale task read the NEW send's data instead
# of the one it meant to check. 15 > 10 structurally guarantees the poll
# always completes first. Same `set_nx` claim shape as the portfolio
# overview cooldown above — a routine double-click/retry here is expected,
# not an abuse signal, so this is deliberately not built on `_enforce_ip`/
# `_trip`. sha256(email) bucket, matching resend-verification's per-address
# bucket above — the raw address never becomes part of a Redis key name.
REPORT_RESEND_COOLDOWN_SECONDS = 900


def _report_resend_key(email: str) -> str:
    return f"rl:report_resend:{hashlib.sha256(email.encode()).hexdigest()}"


def check_report_resend_cooldown(email: str) -> int | None:
    """Claim the manual-resend slot for *email*, or report seconds left to wait.

    Returns `None` and claims the cooldown window if clear to send. Returns
    the remaining seconds (>0) if still in cooldown — the caller must not
    send in that case. Fails closed (503) if the counter store is
    unavailable, consistent with every other limiter in this module.
    """
    key = _report_resend_key(email)
    try:
        claimed = get_backend().set_nx(key, REPORT_RESEND_COOLDOWN_SECONDS)
        if claimed:
            return None
        remaining = get_backend().ttl(key)
    except RateLimitUnavailable:
        logger.exception("rate_limit: report resend cooldown store unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL
        ) from None
    return remaining if remaining > 0 else REPORT_RESEND_COOLDOWN_SECONDS


def release_report_resend_cooldown(email: str) -> None:
    """Undo a claim made by `check_report_resend_cooldown` (PR #338 review
    leftover, blacktomb42): if `regenerate_report` raises AFTER the claim —
    the resend never actually reached `send_report_email` — the caller must
    not still be locked out for the full 15 minutes over a resend that
    never happened. Same best-effort shape as
    `release_portfolio_overview_cooldown`: a store outage here just leaves
    the claim in place (the caller waits out the cooldown as normal), it
    must not itself raise and turn an already-logged regenerate failure
    into a second, unhandled exception.
    """
    try:
        get_backend().delete(_report_resend_key(email))
    except RateLimitUnavailable:
        logger.warning(
            "rate_limit: could not release report resend cooldown for the resolved "
            "recipient (store unavailable) — it will expire naturally"
        )
