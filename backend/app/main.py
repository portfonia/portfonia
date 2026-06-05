"""Portfonia FastAPI entry point."""

from fastapi import FastAPI

from app.core.config import get_settings
from app.routers import holdings, portfolio, reports

settings = get_settings()

app = FastAPI(
    title="Portfonia",
    version="0.0.1",
    docs_url="/docs" if settings.APP_ENV != "production" else None,
)

app.include_router(holdings.router, prefix="/holdings", tags=["holdings"])
app.include_router(portfolio.router, prefix="/portfolio", tags=["portfolio"])
app.include_router(reports.router, prefix="/reports", tags=["reports"])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.APP_ENV}
