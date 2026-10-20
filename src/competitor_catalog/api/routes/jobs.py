"""Job history (every task a worker executed or will execute)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select

from competitor_catalog.api import schemas, views
from competitor_catalog.api.deps import (
    ApiError,
    ConfigsDep,
    MatchingDep,
    PageDep,
    SessionDep,
    competitor_id_filter,
    not_found,
    require_api_key,
)
from competitor_catalog.api.routes.competitors import refusal
from competitor_catalog.db.models import JOB_ACTIVE_STATUSES, Competitor, WorkerJob
from competitor_catalog.services import jobs as job_service

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=schemas.Page[schemas.JobOut], summary="List jobs, newest first")
def list_jobs(
    session: SessionDep,
    page: PageDep,
    competitor: Annotated[str | None, Query(description="competitor key")] = None,
    task: schemas.JobTask | None = None,
    status: Annotated[list[schemas.JobStatus] | None, Query(description="repeatable")] = None,
    active: Annotated[bool | None, Query(description="true = queued/running only")] = None,
):
    stmt = select(WorkerJob).order_by(WorkerJob.created_at.desc(), WorkerJob.id.desc())
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(WorkerJob.competitor_id == competitor_id)
    if task:
        stmt = stmt.where(WorkerJob.task == task)
    if status:
        stmt = stmt.where(WorkerJob.status.in_(status))
    if active is True:
        stmt = stmt.where(WorkerJob.status.in_(JOB_ACTIVE_STATUSES))
    elif active is False:
        stmt = stmt.where(WorkerJob.status.not_in(JOB_ACTIVE_STATUSES))
    result = page.page(session, stmt)
    result["items"] = views.jobs_out(session, result["items"])
    return result


def _job(session, job_id: int) -> WorkerJob:
    job = session.get(WorkerJob, job_id)
    if job is None:
        raise not_found("job", job_id)
    return job


@router.get("/{job_id}", response_model=schemas.JobOut, summary="One job with its live progress")
def get_job(job_id: int, session: SessionDep):
    return views.jobs_out(session, [_job(session, job_id)])[0]


@router.post(
    "/{job_id}/cancel",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Cancel a job",
    responses={409: {"model": schemas.ErrorResponse}},
)
def cancel_job(job_id: int, session: SessionDep):
    job = _job(session, job_id)
    try:
        job_service.request_cancel(session, job)
    except job_service.JobRequestError as exc:
        raise refusal(exc) from None
    session.commit()
    return views.jobs_out(session, [job])[0]


@router.post(
    "/{job_id}/pause",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Pause a running job",
    responses={409: {"model": schemas.ErrorResponse}},
)
def pause_job(job_id: int, session: SessionDep):
    job = _job(session, job_id)
    try:
        job_service.request_pause(session, job)
    except job_service.JobRequestError as exc:
        raise refusal(exc) from None
    session.commit()
    return views.jobs_out(session, [job])[0]


@router.post(
    "/{job_id}/resume",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Resume a paused job",
    responses={409: {"model": schemas.ErrorResponse}},
)
def resume_job(job_id: int, session: SessionDep):
    job = _job(session, job_id)
    try:
        job_service.request_resume(session, job)
    except job_service.JobRequestError as exc:
        raise refusal(exc) from None
    session.commit()
    return views.jobs_out(session, [job])[0]


@router.post(
    "/{job_id}/restart",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Restart an interrupted, failed or cancelled job where it stopped",
    responses={409: {"model": schemas.ErrorResponse}},
)
def restart_job(job_id: int, session: SessionDep, configs: ConfigsDep, matching: MatchingDep):
    """Queues a new job of the same task (``resumed_from_job_id`` = this job) that skips the
    work already done: a catalog sync replays the category pages already fetched (no request)
    and does not refetch the products synced since the stopped job started, a price refresh
    skips the products priced since then, a matching is incremental anyway. Jobs interrupted by a worker restart
    are restarted automatically (``requested_by: "auto_resume"``) unless the server sets
    ``WORKER_AUTO_RESUME=false``."""
    job = _job(session, job_id)
    competitor = session.get(Competitor, job.competitor_id)
    config = configs.get(competitor.key)
    if config is None:
        raise ApiError(409, "competitor_not_configured", f"no configuration file for {competitor.key}")
    try:
        new = job_service.restart_job(session, job, config, requested_by="api", matching=matching)
    except job_service.JobRequestError as exc:
        raise refusal(exc) from None
    session.commit()
    return views.job_out(new, competitor.key)


@router.delete(
    "/{job_id}",
    status_code=204,
    summary="Delete a finished job from the history",
    responses={409: {"model": schemas.ErrorResponse}},
)
def delete_job(job_id: int, session: SessionDep):
    job = _job(session, job_id)
    if job.status in JOB_ACTIVE_STATUSES:
        raise ApiError(409, "job_active", f"job #{job_id} is {job.status}: cancel it first")
    session.delete(job)
    session.commit()
    return Response(status_code=204)
