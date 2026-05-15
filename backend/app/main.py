"""Portfonia FastAPI entry point.

Scaffold only — business routers are mounted in later phases (C: holdings,
D: portfolio, F: reports). Health endpoint exists to validate the stack runs.
"""

from fastapi import FastAPI

from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title="Portfonia",
    version="0.0.1",
    docs_url="/docs" if settings.APP_ENV != "production" else None,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.APP_ENV}
