"""Infrastructure readiness. Must not stamp last_scan_completed_at."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from vigil_app.core.config import get_settings
from vigil_app.core.database import get_session

router = APIRouter()


def _database_ready(session: Session) -> bool:
    try:
        session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _redis_ready() -> bool:
    client: Redis | None = None
    try:
        client = Redis.from_url(
            get_settings().redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        return bool(client.ping())
    except Exception:
        return False
    finally:
        if client is not None:
            client.close()


@router.get("/health/ready")
def ready(session: Session = Depends(get_session)) -> JSONResponse:
    status = "ready" if _database_ready(session) and _redis_ready() else "degraded"
    code = 200 if status == "ready" else 503
    return JSONResponse({"status": status}, status_code=code)
