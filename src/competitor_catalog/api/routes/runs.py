"""Scrape runs (one per executed operation) and their per page/product items."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select

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
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import Competitor, ScrapeRun, ScrapeRunItem
from competitor_catalog.services import jobs as job_service

router = APIRouter(tags=["runs"], dependencies=[Depends(require_api_key)])


def _run(session, run_id: int) -> ScrapeRun:
    run = session.get(ScrapeRun, run_id)
    if run is None:
        raise not_found("run", run_id)
    return run


@router.get("/runs", response_model=schemas.Page[schemas.RunOut], summary="List runs, newest first")
def list_runs(
    session: SessionDep,
    page: PageDep,
    competitor: Annotated[str | None, Query(description="competitor key")] = None,
    operation: schemas.RunOperation | None = None,
    status: schemas.RunStatus | None = None,
    full_scope: bool | None = None,
):
    stmt = select(ScrapeRun).order_by(ScrapeRun.id.desc())
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(ScrapeRun.competitor_id == competitor_id)
    if operation:
        stmt = stmt.where(ScrapeRun.operation == operation)
    if status:
        stmt = stmt.where(ScrapeRun.status == status)
    if full_scope is not None:
        stmt = stmt.where(ScrapeRun.full_scope.is_(full_scope))
    return page.page(session, stmt)


@router.get("/runs/{run_id}", response_model=schemas.RunDetailOut, summary="One run with item counts")
def get_run(run_id: int, session: SessionDep):
    run = _run(session, run_id)
    counts: dict[str, dict[str, int]] = {}
    for item_type, status, n in session.execute(
        select(ScrapeRunItem.item_type, ScrapeRunItem.status, func.count())
        .where(ScrapeRunItem.scrape_run_id == run_id)
        .group_by(ScrapeRunItem.item_type, ScrapeRunItem.status)
    ):
        counts.setdefault(item_type, {})[status] = n
    return {**schemas.RunOut.model_validate(run).model_dump(), "stats": run.stats, "item_counts": counts}


@router.post(
    "/runs/{run_id}/retry-failed",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Retry only the pages that failed in a run",
    responses={409: {"model": schemas.ErrorResponse}},
)
def retry_failed(run_id: int, session: SessionDep, configs: ConfigsDep, matching: MatchingDep):
    """Queues a job on the run's competitor that refetches only the failed pages of the run:
    a ``catalog_sync`` with ``retry_failed_run_id`` for a catalog run (failed product pages,
    including products never created, and failed listing pages with their sub-pages), a
    ``price_refresh`` with ``retry_failed_run_id`` for a price refresh run. ``409
    nothing_to_retry`` when the run has no failed page; ``worker_busy`` as usual."""
    run = _run(session, run_id)
    failed = repo.count_failed_items(session, run_id)
    if not sum(failed.values()):
        raise ApiError(409, "nothing_to_retry", f"run #{run_id} has no failed page")
    competitor = session.get(Competitor, run.competitor_id)
    config = configs.get(competitor.key)
    if config is None:
        raise ApiError(409, "competitor_not_configured", f"no configuration file for {competitor.key}")
    task = "catalog_sync" if run.operation == "catalog_discovery" else "price_refresh"
    options = {"retry_failed_run_id": run_id}
    if task == "catalog_sync":
        options["resume_within_hours"] = 0
    try:
        job = job_service.enqueue_job(session, competitor, config, task, options, requested_by="api", matching=matching)
    except job_service.JobRequestError as exc:
        raise refusal(exc) from None
    session.commit()
    return views.job_out(job, competitor.key)


@router.delete("/runs/{run_id}", status_code=204, summary="Delete a finished run and its items")
def delete_run(run_id: int, session: SessionDep):
    """Observations of the run are kept (their ``scrape_run_id`` becomes null)."""
    run = _run(session, run_id)
    if run.status == "running":
        raise ApiError(409, "run_active", f"run #{run_id} is still running")
    session.delete(run)
    session.commit()
    return Response(status_code=204)


@router.get("/runs/{run_id}/items", response_model=schemas.Page[schemas.RunItemOut], summary="Items of a run")
def run_items(
    run_id: int,
    session: SessionDep,
    page: PageDep,
    status: Annotated[list[schemas.ItemStatus] | None, Query(description="repeatable")] = None,
    item_type: schemas.ItemType | None = None,
):
    _run(session, run_id)
    return page.page(session, _items_query(status, item_type).where(ScrapeRunItem.scrape_run_id == run_id))


@router.get(
    "/run-items", response_model=schemas.Page[schemas.RunItemOut], summary="Items across runs (e.g. every recent error)"
)
def all_run_items(
    session: SessionDep,
    page: PageDep,
    competitor: str | None = None,
    status: Annotated[list[schemas.ItemStatus] | None, Query(description="repeatable, e.g. failed")] = None,
    item_type: schemas.ItemType | None = None,
    product_id: int | None = None,
    url: str | None = None,
):
    stmt = _items_query(status, item_type)
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.join(ScrapeRun, ScrapeRun.id == ScrapeRunItem.scrape_run_id).where(
            ScrapeRun.competitor_id == competitor_id
        )
    if product_id is not None:
        stmt = stmt.where(ScrapeRunItem.product_id == product_id)
    if url:
        stmt = stmt.where(ScrapeRunItem.url == url)
    return page.page(session, stmt)


def _items_query(status, item_type):
    stmt = select(ScrapeRunItem).order_by(ScrapeRunItem.id.desc())
    if status:
        stmt = stmt.where(ScrapeRunItem.status.in_(status))
    if item_type:
        stmt = stmt.where(ScrapeRunItem.item_type == item_type)
    return stmt
