"""ORM -> response model conversion shared by the routes."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from competitor_catalog.api import schemas
from competitor_catalog.core.config import CompetitorConfig
from competitor_catalog.db.models import (
    JOB_ACTIVE_STATUSES,
    Category,
    Competitor,
    Notification,
    Product,
    ScrapeObservation,
    WorkerJob,
)
from competitor_catalog.matching.service import MatchingRuntime
from competitor_catalog.services import jobs as job_service
from competitor_catalog.services.estimates import estimate_job


def job_out(job: WorkerJob, competitor_key: str) -> schemas.JobOut:
    duration = None
    if job.started_at:
        end = job.finished_at or job.heartbeat_at
        duration = (end - job.started_at).total_seconds() if end else None
    return schemas.JobOut(
        id=job.id,
        competitor_id=job.competitor_id,
        competitor_key=competitor_key,
        task=job.task,
        status=job.status,
        options=job.options,
        requested_by=job.requested_by,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        heartbeat_at=job.heartbeat_at,
        cancel_requested_at=job.cancel_requested_at,
        pause_requested_at=job.pause_requested_at,
        paused_at=job.paused_at,
        scrape_run_id=job.scrape_run_id,
        resumed_from_job_id=job.resumed_from_job_id,
        step=job.step,
        step_label=job_service.STEP_LABELS.get(job.step or "", job.step),
        step_done=job.step_done,
        step_goal=job.step_goal,
        progress=schemas.ProgressOut(**(job.progress or {})),
        counters=job.counters or {},
        estimate=schemas.EstimateOut(**job.estimate) if job.estimate else None,
        error=job.error,
        duration_seconds=duration,
    )


def competitor_keys(session: Session) -> dict[int, str]:
    return dict(session.execute(select(Competitor.id, Competitor.key)).all())


def jobs_out(session: Session, jobs: Iterable[WorkerJob]) -> list[schemas.JobOut]:
    keys = competitor_keys(session)
    return [job_out(job, keys.get(job.competitor_id, "?")) for job in jobs]


def worker_out(session: Session, competitor: Competitor, matching: MatchingRuntime | None = None) -> schemas.WorkerOut:
    active = job_service.active_job(session, competitor.id)
    last = session.scalar(
        select(WorkerJob)
        .where(WorkerJob.competitor_id == competitor.id, WorkerJob.status.not_in(JOB_ACTIVE_STATUSES))
        .order_by(WorkerJob.created_at.desc(), WorkerJob.id.desc())
        .limit(1)
    )
    if active is None:
        state = "idle"
    elif active.status == "queued":
        state = "queued"
    elif active.cancel_requested_at is not None:
        state = "stopping"
    elif active.status == "paused":
        state = "paused" if active.pause_requested_at is not None else "resuming"
    elif active.pause_requested_at is not None:
        state = "pausing"
    else:
        state = "running"
    tasks = {
        task: schemas.TaskAvailability(allowed=reason is None, reason=reason)
        for task, reason in job_service.allowed_tasks(session, competitor, matching).items()
    }
    return schemas.WorkerOut(
        competitor_key=competitor.key,
        state=state,
        job=job_out(active, competitor.key) if active else None,
        last_job=job_out(last, competitor.key) if last else None,
        tasks=tasks,
    )


def competitor_stats(session: Session, competitor_id: int) -> schemas.CompetitorStats:
    categories = session.scalar(
        select(func.count()).select_from(Category).where(Category.competitor_id == competitor_id)
    )
    rows = dict(
        session.execute(
            select(Product.status, func.count()).where(Product.competitor_id == competitor_id).group_by(Product.status)
        ).all()
    )
    observations, last_obs = session.execute(
        select(func.count(ScrapeObservation.id), func.max(ScrapeObservation.observed_at))
        .join(Product, Product.id == ScrapeObservation.product_id)
        .where(Product.competitor_id == competitor_id)
    ).one()
    return schemas.CompetitorStats(
        categories=categories,
        products_active=rows.get("active", 0),
        products_missing=rows.get("missing", 0),
        observations=observations,
        last_price_observed_at=last_obs,
    )


def competitor_out(session: Session, competitor: Competitor, matching: MatchingRuntime | None = None) -> dict[str, Any]:
    return {
        "id": competitor.id,
        "key": competitor.key,
        "name": competitor.name,
        "base_url": competitor.base_url,
        "adapter": competitor.adapter,
        "enabled": competitor.enabled,
        "last_full_catalog_at": competitor.last_full_catalog_at,
        "last_full_catalog_run_id": competitor.last_full_catalog_run_id,
        "last_full_catalog_status": competitor.last_full_catalog_status,
        "last_price_refresh_at": competitor.last_price_refresh_at,
        "created_at": competitor.created_at,
        "updated_at": competitor.updated_at,
        "stats": competitor_stats(session, competitor.id),
        "worker": worker_out(session, competitor, matching),
    }


def competitor_detail(
    session: Session, competitor: Competitor, config: CompetitorConfig | None, matching: MatchingRuntime | None = None
) -> dict[str, Any]:
    data = competitor_out(session, competitor, matching)
    data["config"] = competitor.config
    estimates = {}
    if config is not None:
        for task in job_service.TASKS:
            if task == "matching" and (matching is None or competitor.key == matching.config.reference_competitor):
                continue
            options = job_service.normalize_options(task, {})
            estimates[task] = schemas.EstimateOut(
                **estimate_job(
                    session, competitor.id, config, task, options, matching_config=matching.config if matching else None
                ).as_dict()
            )
    data["estimates"] = estimates
    return data


# --------------------------------------------------------------------------- prices


def price_summaries(session: Session, product_ids: list[int]) -> dict[int, schemas.PriceSummary]:
    if not product_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT * FROM latest_price_observations WHERE product_id = ANY(:ids) "
            "ORDER BY product_id, min_quantity NULLS FIRST, quantity NULLS FIRST, id"
        ),
        {"ids": product_ids},
    ).mappings()
    grouped: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[row["product_id"]].append(row)
    result = {}
    for product_id, tiers in grouped.items():
        first = tiers[0]
        result[product_id] = schemas.PriceSummary(
            observed_at=first["observed_at"],
            currency=first["currency"],
            from_price=first["price"],
            min_price=min(t["price"] for t in tiers),
            tiers=len(tiers),
            price_basis=first["price_basis"],
            price_unit=first["price_unit"],
            price_unit_quantity=first["price_unit_quantity"],
            quantity_unit=first["quantity_unit"],
            tax_mode=first["tax_mode"],
            displayed=(first["metadata"] or {}).get("displayed", True) is not False,
        )
    return result


def category_path(session: Session, category_id: int | None) -> list[Category]:
    path: list[Category] = []
    seen: set[int] = set()
    while category_id is not None and category_id not in seen:
        seen.add(category_id)
        category = session.get(Category, category_id)
        if category is None:
            break
        path.append(category)
        category_id = category.parent_id
    return list(reversed(path))


def notification_out(notification: Notification) -> schemas.NotificationOut:
    return schemas.NotificationOut.model_validate(
        {
            **{c: getattr(notification, c) for c in schemas.NotificationOut.model_fields if c != "read"},
            "read": notification.read_at is not None,
        }
    )
