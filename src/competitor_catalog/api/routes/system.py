from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text

from competitor_catalog import __version__
from competitor_catalog.api import schemas
from competitor_catalog.api.deps import ApiError, SessionDep, require_api_key
from competitor_catalog.db.models import (
    JOB_ACTIVE_STATUSES,
    Category,
    Competitor,
    Notification,
    Product,
    ScrapeObservation,
    ScrapeRun,
    ServiceHeartbeat,
    WorkerJob,
)
from competitor_catalog.services.dispatcher import HEARTBEAT_NAME
from competitor_catalog.services.runs import utcnow

router = APIRouter(tags=["system"])
WORKER_OFFLINE_AFTER = timedelta(seconds=60)


@router.get("/health", response_model=schemas.HealthOut, summary="Liveness of the API, database and worker")
def health(session: SessionDep) -> schemas.HealthOut:
    """No authentication. ``worker.online`` is false when the worker process has not sent a
    heartbeat for 60 s: jobs can still be queued but will not start until it is back."""
    try:
        session.execute(text("SELECT 1"))
        beat = session.get(ServiceHeartbeat, HEARTBEAT_NAME)
    except Exception:
        return schemas.HealthOut(
            status="degraded", version=__version__, database=False, worker=schemas.WorkerServiceStatus(online=False)
        )
    worker = schemas.WorkerServiceStatus(online=False)
    if beat is not None:
        worker = schemas.WorkerServiceStatus(
            online=utcnow() - beat.heartbeat_at < WORKER_OFFLINE_AFTER,
            instance=beat.instance,
            started_at=beat.started_at,
            heartbeat_at=beat.heartbeat_at,
            running_jobs=beat.details.get("running_jobs", []),
            auto_schedule=bool(beat.details.get("auto_schedule")),
        )
    return schemas.HealthOut(
        status="ok" if worker.online else "degraded", version=__version__, database=True, worker=worker
    )


@router.get(
    "/stats", response_model=schemas.StatsOut, dependencies=[Depends(require_api_key)], summary="Global counters"
)
def stats(session: SessionDep) -> schemas.StatsOut:
    def count(model, *where):
        return session.scalar(select(func.count()).select_from(model).where(*where))

    return schemas.StatsOut(
        competitors=count(Competitor),
        categories=count(Category),
        products_active=count(Product, Product.status == "active"),
        products_missing=count(Product, Product.status == "missing"),
        observations=count(ScrapeObservation),
        runs=count(ScrapeRun),
        active_jobs=count(WorkerJob, WorkerJob.status.in_(JOB_ACTIVE_STATUSES)),
        unread_notifications=count(Notification, Notification.read_at.is_(None)),
    )


@router.get(
    "/scrapedo/account", dependencies=[Depends(require_api_key)], summary="Scrape.do subscription and remaining credits"
)
def scrapedo_account(request: Request) -> dict:
    """Proxied from Scrape.do (limited by Scrape.do to 10 calls per minute)."""
    from competitor_catalog.http.scrapedo import ScrapeDoClient

    key = request.app.state.settings.scrapedo_api_key
    if not key:
        raise ApiError(503, "scrapedo_not_configured", "SCRAPEDO_API_KEY is not set on the API service")
    with ScrapeDoClient(key, max_attempts=1) as client:
        try:
            return client.account_info()
        except Exception as exc:
            raise ApiError(502, "scrapedo_unavailable", f"Scrape.do account endpoint failed: {exc}") from exc
