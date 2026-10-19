"""Scrape runs (one per executed operation) and their per page/product items."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select

from competitor_catalog.api import schemas
from competitor_catalog.api.deps import ApiError, PageDep, SessionDep, competitor_id_filter, not_found, require_api_key
from competitor_catalog.db.models import ScrapeRun, ScrapeRunItem

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
