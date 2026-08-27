"""Fixed-window rate limits for signup and invite minting (issue #190)."""

from __future__ import annotations

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
            body=(
                f"{n} {noun} attempts today (UTC {day}); threshold {limit}. "
                "Not auto-blocked."
            ),
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
