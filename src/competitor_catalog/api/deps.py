"""Shared API plumbing: error format, authentication, database session, pagination."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, Query, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from competitor_catalog.core.config import CompetitorConfig
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import Competitor
from competitor_catalog.matching.service import MatchingRuntime


class ApiError(Exception):
    """Rendered as ``{"error": {"code", "message", "details"}}`` with the given HTTP status."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def not_found(what: str, key: Any) -> ApiError:
    return ApiError(404, "not_found", f"{what} {key!r} not found")


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


# --------------------------------------------------------------------------- auth

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="API key (or Authorization: Bearer)")
_bearer = HTTPBearer(auto_error=False)


def require_api_key(
    request: Request,
    api_key: Annotated[str | None, Depends(_api_key_header)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    expected: str | None = request.app.state.api_key
    if expected is None:  # API_AUTH_DISABLED=true (local development only)
        return
    provided = api_key or (bearer.credentials if bearer else None)
    if not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
        raise ApiError(401, "unauthorized", "missing or invalid API key (X-API-Key header)")


# --------------------------------------------------------------------------- session


def get_session(request: Request) -> Iterator[Session]:
    """One session per request; mutating endpoints commit explicitly before responding."""
    session = request.app.state.factory()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def get_configs(request: Request) -> dict[str, CompetitorConfig]:
    return request.app.state.configs


ConfigsDep = Annotated[dict[str, CompetitorConfig], Depends(get_configs)]


def get_matching(request: Request) -> MatchingRuntime:
    return request.app.state.matching


MatchingDep = Annotated[MatchingRuntime, Depends(get_matching)]


def competitor_or_404(session: Session, key: str) -> Competitor:
    competitor = repo.get_competitor(session, key)
    if competitor is None and key.isdigit():
        competitor = session.get(Competitor, int(key))
    if competitor is None:
        raise not_found("competitor", key)
    return competitor


def competitor_id_filter(session: Session, key: str | None) -> int | None:
    return competitor_or_404(session, key).id if key else None


# --------------------------------------------------------------------------- pagination


class Pagination:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=500, description="page size")] = 50,
        offset: Annotated[int, Query(ge=0, description="number of items to skip")] = 0,
    ):
        self.limit = limit
        self.offset = offset

    def page(self, session: Session, stmt: Select) -> dict[str, Any]:
        total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
        items = list(session.scalars(stmt.limit(self.limit).offset(self.offset)))
        return {"items": items, "total": total, "limit": self.limit, "offset": self.offset}


PageDep = Annotated[Pagination, Depends()]
