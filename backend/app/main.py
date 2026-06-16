"""Portfonia FastAPI entry point."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import holdings, portfolio, reports

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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.APP_ENV}
