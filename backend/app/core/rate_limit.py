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
