"""Portfonia FastAPI entry point."""

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.routers import admin, auth, holdings, investment_context, me, portfolio, reports

# Without this the root logger defaults to WARNING and every logger.info() in the
# service layer (notably the _call_llm finish_reason / token-usage / cost
# instrumentation) is silently dropped — the calls exist but never reach
# .run/uvicorn.log. Configure once, at import, before any router runs. (I-DEBT-3)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# httpx logs full URLs at INFO level, which leaks API keys in query strings
# (e.g. FRED api_key= param). Suppress to WARNING so the transport-level
# GET lines never reach the log file.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

settings = get_settings()

app = FastAPI(
    title="Portfonia",
    version="0.0.1",
    docs_url="/docs" if settings.APP_ENV != "production" else None,
)

# The Next.js UI calls this API from a separate origin (FRONTEND_URL). Without
# CORS the browser blocks those requests. Scoped to the configured frontend
# origin only — not a wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(holdings.router, prefix="/holdings", tags=["holdings"])
app.include_router(portfolio.router, prefix="/portfolio", tags=["portfolio"])
app.include_router(reports.router, prefix="/reports", tags=["reports"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(
    investment_context.router, prefix="/investment-context", tags=["investment-context"]
)
app.include_router(me.router, prefix="/me", tags=["me"])

# Pydantic/FastAPI 422 bodies include `"input"`. SecretStr does not strip it
# (validation runs on the raw string). Redact known secret fields so a public
# endpoint cannot echo a submitted password.
_SECRET_BODY_FIELDS = frozenset({"password"})


def _redact_secret_validation_errors(errors: list[object]) -> list[object]:
    redacted: list[object] = []
    for err in errors:
        if not isinstance(err, dict):
            redacted.append(err)
            continue
        loc = err.get("loc", ())
        if isinstance(loc, list | tuple) and any(part in _SECRET_BODY_FIELDS for part in loc):
            redacted.append({**err, "input": "[redacted]"})
        else:
            redacted.append(err)
    return redacted


@app.exception_handler(RequestValidationError)
async def _hide_secrets_in_422(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = jsonable_encoder(exc.errors())
    if not isinstance(errors, list):
        errors = [errors]
    return JSONResponse(
        status_code=422,
        content={"detail": _redact_secret_validation_errors(errors)},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.APP_ENV}
