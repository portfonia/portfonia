"""Vigil FastAPI entry point. Isolated from Portfonia's app.main."""

import logging

from fastapi import FastAPI

from vigil_app.core.config import get_settings
from vigil_app.routers import health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

settings = get_settings()

app = FastAPI(
    title="Vigil",
    version="0.0.1",
    docs_url="/docs" if settings.APP_ENV != "production" else None,
)

app.include_router(health.router)
