"""Portfonia FastAPI entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers import holdings, portfolio, reports

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
