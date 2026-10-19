"""Competitors and their worker (one worker slot per competitor)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends
from sqlalchemy import select

from competitor_catalog.api import schemas, views
from competitor_catalog.api.deps import ApiError, ConfigsDep, SessionDep, competitor_or_404, require_api_key
from competitor_catalog.db.models import Competitor
from competitor_catalog.services import jobs as job_service
from competitor_catalog.services.estimates import estimate_job

router = APIRouter(prefix="/competitors", tags=["competitors & workers"], dependencies=[Depends(require_api_key)])

REFUSAL_STATUS = {
    "worker_busy": 409,
    "catalog_required": 409,
    "competitor_disabled": 409,
    "job_not_active": 409,
    "job_not_running": 409,
    "job_not_paused": 409,
    "job_stopping": 409,
    "invalid_options": 422,
    "invalid_task": 422,
}
JobBody = Annotated[schemas.JobIn, Body(examples=[{"task": "catalog_sync"}, {"task": "price_refresh"}])]


def refusal(exc: job_service.JobRequestError) -> ApiError:
    return ApiError(REFUSAL_STATUS.get(exc.code, 409), exc.code, exc.message, {"job_id": exc.job_id})


@router.get("", response_model=list[schemas.CompetitorOut], summary="List competitors with their worker state")
def list_competitors(session: SessionDep):
    competitors = session.scalars(select(Competitor).order_by(Competitor.name))
    return [views.competitor_out(session, c) for c in competitors]


@router.get("/{key}", response_model=schemas.CompetitorDetailOut, summary="One competitor (key or id)")
def get_competitor(key: str, session: SessionDep, configs: ConfigsDep):
    """Includes the estimated duration/credits of each task with default options."""
    competitor = competitor_or_404(session, key)
    return views.competitor_detail(session, competitor, configs.get(competitor.key))


@router.get("/{key}/worker", response_model=schemas.WorkerOut, summary="Worker state, current job and progress")
def get_worker(key: str, session: SessionDep):
    """Poll this every 2-5 s while ``state`` is ``queued``/``running``/``stopping``."""
    return views.worker_out(session, competitor_or_404(session, key))


@router.post(
    "/{key}/worker/start",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Start the worker on a task (catalog_sync or price_refresh)",
    responses={409: {"model": schemas.ErrorResponse}, 422: {"model": schemas.ErrorResponse}},
)
def start_worker(key: str, body: JobBody, session: SessionDep, configs: ConfigsDep):
    """Queues a job; the worker process picks it up within a few seconds.

    Refusals (HTTP 409): ``worker_busy`` (the competitor already has a queued/running job,
    whatever its task), ``catalog_required`` (price refresh before any full catalog sync),
    ``competitor_disabled``.
    """
    competitor = competitor_or_404(session, key)
    config = configs.get(competitor.key)
    if config is None:
        raise ApiError(409, "competitor_not_configured", f"no configuration file for {competitor.key}")
    try:
        job = job_service.enqueue_job(
            session, competitor, config, body.task, body.options.model_dump(), requested_by="api"
        )
    except job_service.JobRequestError as exc:
        raise refusal(exc) from None
    session.commit()
    return views.job_out(job, competitor.key)


@router.post(
    "/{key}/worker/stop",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Stop the active job of the competitor",
    responses={409: {"model": schemas.ErrorResponse}},
)
def stop_worker(key: str, session: SessionDep):
    """A queued job is cancelled at once; a running job stops after the current page
    (``state`` becomes ``stopping``, then the job ends as ``cancelled``)."""
    competitor = competitor_or_404(session, key)
    job = _active_or_409(session, competitor)
    job_service.request_cancel(session, job)
    session.commit()
    return views.job_out(job, competitor.key)


def _active_or_409(session, competitor):
    job = job_service.active_job(session, competitor.id)
    if job is None:
        raise ApiError(409, "worker_idle", f"{competitor.key} has no active job")
    return job


@router.post(
    "/{key}/worker/pause",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Pause the running job (resumable)",
    responses={409: {"model": schemas.ErrorResponse}},
)
def pause_worker(key: str, session: SessionDep):
    """The job pauses after the current page (``state``: ``pausing`` then ``paused``) and keeps
    its place and the competitor's worker slot: no other task can start until it is resumed or
    stopped. A paused job does not survive a worker restart/redeploy (it ends ``interrupted``)."""
    competitor = competitor_or_404(session, key)
    try:
        job = job_service.request_pause(session, _active_or_409(session, competitor))
    except job_service.JobRequestError as exc:
        raise refusal(exc) from None
    session.commit()
    return views.job_out(job, competitor.key)


@router.post(
    "/{key}/worker/resume",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Resume a paused job",
    responses={409: {"model": schemas.ErrorResponse}},
)
def resume_worker(key: str, session: SessionDep):
    """``state`` becomes ``resuming`` then ``running`` within a few seconds."""
    competitor = competitor_or_404(session, key)
    try:
        job = job_service.request_resume(session, _active_or_409(session, competitor))
    except job_service.JobRequestError as exc:
        raise refusal(exc) from None
    session.commit()
    return views.job_out(job, competitor.key)


@router.post(
    "/{key}/worker/estimate",
    response_model=schemas.EstimateOut,
    summary="Estimate the duration/credits of a task without starting it",
)
def estimate(key: str, body: JobBody, session: SessionDep, configs: ConfigsDep):
    competitor = competitor_or_404(session, key)
    config = configs.get(competitor.key)
    if config is None:
        raise ApiError(409, "competitor_not_configured", f"no configuration file for {competitor.key}")
    try:
        options = job_service.normalize_options(body.task, body.options.model_dump())
    except job_service.JobRequestError as exc:
        raise refusal(exc) from None
    return schemas.EstimateOut(**estimate_job(session, competitor.id, config, body.task, options).as_dict())
