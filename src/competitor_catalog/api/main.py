"""FastAPI application factory (``catalog-scraper api`` serves it with uvicorn)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from competitor_catalog import __version__
from competitor_catalog.api import events, mcp
from competitor_catalog.api.deps import ApiError, error_body
from competitor_catalog.api.routes import (
    categories,
    competitors,
    jobs,
    notifications,
    observations,
    products,
    runs,
    stats,
    system,
)
from competitor_catalog.api.routes import matching as matching_routes
from competitor_catalog.app import build_matching_runtime, session_factory
from competitor_catalog.core.config import load_competitor_configs
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.session import transaction
from competitor_catalog.logging_setup import register_secret
from competitor_catalog.matching.service import MatchingRuntime
from competitor_catalog.settings import Settings, SettingsError

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
DESCRIPTION = """
Competitor catalog scraper API: competitors and their worker (catalog sync / price
refresh / product matching), scraped categories, products and price history, runs,
matching of competitor products with the reference catalog (Antalis), notifications.

Authentication: send the API key in the `X-API-Key` header (or `Authorization: Bearer`).
MCP server (every endpoint as a tool): `POST /api/v1/mcp?api_key=<key>` (Streamable HTTP).
Errors: `{"error": {"code", "message", "details"}}`. Full guide: `docs/api/` in the repository.
"""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def create_app(
    settings: Settings,
    factory: sessionmaker[Session] | None = None,
    *,
    api_key: str | None = None,
    auth_disabled: bool | None = None,
    sync_competitors: bool = True,
    matching: MatchingRuntime | None = None,
) -> FastAPI:
    api_key = api_key if api_key is not None else (os.environ.get("API_KEY") or None)
    auth_disabled = _truthy(os.environ.get("API_AUTH_DISABLED")) if auth_disabled is None else auth_disabled
    if api_key is None and not auth_disabled:
        raise SettingsError("API_KEY is not set (or set API_AUTH_DISABLED=true for local development only)")
    if auth_disabled:
        log.warning("API authentication is DISABLED")
    register_secret(api_key)  # the MCP endpoint receives it in the URL (access logs)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.events.stop()

    app = FastAPI(
        lifespan=lifespan,
        title="Competitor catalog API",
        version=__version__,
        description=DESCRIPTION,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    app.state.settings = settings
    app.state.factory = factory or session_factory(settings)
    app.state.api_key = None if auth_disabled else api_key
    app.state.configs = load_competitor_configs(settings.competitor_config_dir)
    app.state.matching = matching or build_matching_runtime(settings)
    app.state.events = events.EventHub(app)
    if sync_competitors:  # configured competitors appear at once
        with transaction(app.state.factory) as session:
            for config in app.state.configs.values():
                repo.upsert_competitor(session, config)

    origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=origins != ["*"],
    )

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message, exc.details), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            error_body("validation_error", "invalid request", jsonable_encoder(exc.errors())), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(error_body(code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled API error")
        return JSONResponse(error_body("internal_error", f"{type(exc).__name__}"), status_code=500)

    routers = (
        system,
        stats,
        competitors,
        jobs,
        categories,
        products,
        observations,
        runs,
        matching_routes,
        notifications,
        events,
    )
    for module in routers:
        app.include_router(module.router, prefix=API_PREFIX)
    app.include_router(mcp.router, prefix=API_PREFIX)  # last: its tools are the routes above
    return app
