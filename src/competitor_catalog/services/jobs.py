"""Worker jobs: one task (catalog sync, price refresh or product matching) for one competitor.

Lifecycle: ``queued`` (created by the API) -> ``running`` (claimed by the worker process)
-> ``succeeded`` | ``partial`` | ``failed`` | ``cancelled`` | ``interrupted``.

Rules enforced here and by the database (partial unique index on ``worker_jobs``):

* a competitor has at most one queued/running job, whatever its task;
* a price refresh needs a previous full catalog sync (``competitors.last_full_catalog_at``);
* a matching needs a full catalog sync of the competitor *and* of the reference catalog;
* nothing runs unless a job was requested (or the opt-in scheduler is enabled);
* a job stopped by a worker restart (``interrupted``) can be restarted where it stopped
  (:func:`restart_job`), automatically at worker start-up unless ``WORKER_AUTO_RESUME=false``
  (:func:`auto_resume_interrupted`, at most ``AUTO_RESUME_MAX`` times in a row).
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, fields
from datetime import timedelta
from typing import Any

from sqlalchemy import exists, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.core.adapter import load_adapter
from competitor_catalog.core.config import CompetitorConfig
from competitor_catalog.db.models import (
    JOB_ACTIVE_STATUSES,
    Category,
    Competitor,
    Notification,
    ScrapeRun,
    WorkerJob,
)
from competitor_catalog.db.session import transaction
from competitor_catalog.http.base import Fetcher
from competitor_catalog.matching import service as matching_service
from competitor_catalog.matching.embeddings import EmbeddingError
from competitor_catalog.matching.service import MatchingOptions, MatchingRun, MatchingRuntime
from competitor_catalog.services.catalog_sync import CatalogSync, CatalogSyncOptions
from competitor_catalog.services.estimates import Estimate, estimate_job
from competitor_catalog.services.notifications import notify
from competitor_catalog.services.price_refresh import PriceRefresh, PriceRefreshOptions
from competitor_catalog.services.runs import RunCancelled, RunInterrupted, RunObserver, RunStopped, utcnow

log = logging.getLogger(__name__)

TASKS = ("catalog_sync", "price_refresh", "matching")
TASK_LABELS = {"catalog_sync": "Catalog sync", "price_refresh": "Price refresh", "matching": "Product matching"}
STEP_LABELS = {
    "queued": "Waiting for the worker",
    "starting": "Starting",
    "crawl_listings": "Crawling category/listing pages",
    "sync_products": "Syncing product pages (details + prices)",
    "select_products": "Selecting products",
    "refresh_prices": "Refreshing product prices",
    "detect_changes": "Detecting changed products (obsolete matches)",
    "embed": "Computing embeddings (Voyage AI)",
    "match": "Scoring candidates against the reference catalog",
    "finalize": "Finalizing",
    "done": "Done",
}
TASK_STEPS = {
    "catalog_sync": ("crawl_listings", "sync_products", "finalize"),
    "price_refresh": ("refresh_prices",),
    "matching": ("detect_changes", "embed", "match", "finalize"),
}
OPTION_CLASSES = {
    "catalog_sync": CatalogSyncOptions,
    "price_refresh": PriceRefreshOptions,
    "matching": MatchingOptions,
}
# Default for catalog syncs started by the API: products whose details were synced less
# than 48 h ago (e.g. by an interrupted previous run) are confirmed without being refetched.
DEFAULT_CATALOG_RESUME_HOURS = 48.0
FAILED_ITEM_STATUSES = ("failed", "not_found")
ERROR_BURST = 10  # consecutive failed pages -> immediate error notification
MAX_CONSECUTIVE_FAILURES = 50  # -> the job stops (site blocking us, proxy outage...)
ERROR_DIGEST_SECONDS = 900  # page errors are notified in digests at most every 15 min
RESTARTABLE_STATUSES = ("interrupted", "failed", "cancelled")
AUTO_RESUME_MAX = 3  # consecutive automatic restarts of the same work (crash loop guard)
AUTO_RESUME_WINDOW = timedelta(hours=24)  # older interruptions are not resumed automatically


class JobRequestError(Exception):
    """A job request the API must refuse; ``code`` is stable and documented."""

    def __init__(self, code: str, message: str, *, job_id: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.job_id = job_id


class RunGuardStopped(RunStopped):
    """Stopped by a safety guard (too many consecutive failures)."""


# --------------------------------------------------------------------------- options


def normalize_options(task: str, options: dict[str, Any] | None) -> dict[str, Any]:
    """Validate task options against the service option dataclasses; returns a JSON-able dict."""
    if task not in TASKS:
        raise JobRequestError("invalid_task", f"unknown task {task!r}; expected one of {list(TASKS)}")
    options = {k: v for k, v in (options or {}).items() if v is not None}
    cls = OPTION_CLASSES[task]
    allowed = {f.name for f in fields(cls)}
    unknown = set(options) - allowed
    if unknown:
        raise JobRequestError("invalid_options", f"unknown options for {task}: {sorted(unknown)}")
    if task == "catalog_sync":
        options.setdefault("resume_within_hours", DEFAULT_CATALOG_RESUME_HOURS)
    try:
        return asdict(cls(**options))
    except (TypeError, ValueError) as exc:
        raise JobRequestError("invalid_options", str(exc)) from exc


def build_options(task: str, options: dict[str, Any]) -> CatalogSyncOptions | PriceRefreshOptions | MatchingOptions:
    cls = OPTION_CLASSES[task]
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in options.items() if k in allowed})


def is_full_scope(task: str, options: dict[str, Any]) -> bool:
    return task == "catalog_sync" and build_options(task, options).is_full_scope


# --------------------------------------------------------------------------- API side


def active_job(session: Session, competitor_id: int) -> WorkerJob | None:
    return session.scalar(
        select(WorkerJob).where(WorkerJob.competitor_id == competitor_id, WorkerJob.status.in_(JOB_ACTIVE_STATUSES))
    )


def allowed_tasks(
    session: Session, competitor: Competitor, matching: MatchingRuntime | None = None
) -> dict[str, str | None]:
    """task -> None when it can be started now, else the refusal code."""
    busy = active_job(session, competitor.id) is not None
    result: dict[str, str | None] = {}
    for task in TASKS:
        if not competitor.enabled:
            result[task] = "competitor_disabled"
        elif task == "matching" and (code := matching_service.refusal(session, competitor, matching)):
            result[task] = code
        elif busy:
            result[task] = "worker_busy"
        elif task == "price_refresh" and competitor.last_full_catalog_at is None:
            result[task] = "catalog_required"
        else:
            result[task] = None
    return result


def enqueue_job(
    session: Session,
    competitor: Competitor,
    config: CompetitorConfig,
    task: str,
    options: dict[str, Any] | None = None,
    *,
    requested_by: str = "api",
    matching: MatchingRuntime | None = None,
    resumed_from_job_id: int | None = None,
) -> WorkerJob:
    options = normalize_options(task, options)
    _check_retry_run(session, competitor, task, options)
    _check_category(session, competitor, options)
    if not competitor.enabled:
        raise JobRequestError("competitor_disabled", f"competitor {competitor.key} is disabled in its config")
    if task == "matching" and (code := matching_service.refusal(session, competitor, matching)):
        raise JobRequestError(code, f"{competitor.key}: {matching_service.REFUSALS[code]}")
    if task == "price_refresh" and competitor.last_full_catalog_at is None:
        raise JobRequestError(
            "catalog_required", f"{competitor.key}: a full catalog sync must succeed before a price refresh"
        )
    current = active_job(session, competitor.id)
    if current is not None:
        raise JobRequestError(
            "worker_busy",
            f"{competitor.key} already has a {current.status} {current.task} job (#{current.id})",
            job_id=current.id,
        )
    estimate = estimate_job(
        session, competitor.id, config, task, options, matching_config=matching.config if matching else None
    )
    job = WorkerJob(
        competitor_id=competitor.id,
        task=task,
        status="queued",
        options=options,
        requested_by=requested_by,
        step="queued",
        estimate=estimate.as_dict(),
        progress=initial_progress(task, estimate),
        resumed_from_job_id=resumed_from_job_id,
    )
    try:
        with session.begin_nested():  # the unique index settles concurrent requests
            session.add(job)
            session.flush()
    except IntegrityError:
        raise JobRequestError("worker_busy", f"{competitor.key} already has an active job") from None
    return job


def _check_retry_run(session: Session, competitor: Competitor, task: str, options: dict[str, Any]) -> None:
    run_id = options.get("retry_failed_run_id")
    if run_id is None:
        return
    run = session.get(ScrapeRun, run_id)
    if run is None or run.competitor_id != competitor.id:
        raise JobRequestError("invalid_options", f"run #{run_id} is not a run of {competitor.key}")
    if task == "catalog_sync" and run.operation != "catalog_discovery":
        raise JobRequestError("invalid_options", f"run #{run_id} is not a catalog sync: retry it with a price refresh")


def _check_category(session: Session, competitor: Competitor, options: dict[str, Any]) -> None:
    category_id = options.get("category_id")
    if category_id is None:
        return
    category = session.get(Category, category_id)
    if category is None or category.competitor_id != competitor.id:
        raise JobRequestError("invalid_options", f"category #{category_id} is not a category of {competitor.key}")


def restart_options(job: WorkerJob) -> dict[str, Any]:
    """Options of a job that continues ``job`` where it stopped: what it already did is skipped
    (products synced / priced since it started; matching is incremental anyway)."""
    options = dict(job.options or {})
    started = job.started_at or job.created_at
    hours = round((utcnow() - started).total_seconds() / 3600 + 0.05, 2)
    if job.task == "catalog_sync":
        options["resume_within_hours"] = max(float(options.get("resume_within_hours") or 0), hours)
    elif job.task == "price_refresh":
        options["skip_fresh_hours"] = max(float(options.get("skip_fresh_hours") or 0), hours)
    return options


def restart_job(
    session: Session,
    job: WorkerJob,
    config: CompetitorConfig,
    *,
    requested_by: str = "api",
    matching: MatchingRuntime | None = None,
) -> WorkerJob:
    """Queue a job continuing an interrupted / failed / cancelled one (same task)."""
    if job.status not in RESTARTABLE_STATUSES:
        raise JobRequestError(
            "job_not_restartable",
            f"job #{job.id} is {job.status}: only {', '.join(RESTARTABLE_STATUSES)} jobs can be restarted",
            job_id=job.id,
        )
    competitor = session.get(Competitor, job.competitor_id)
    return enqueue_job(
        session,
        competitor,
        config,
        job.task,
        restart_options(job),
        requested_by=requested_by,
        matching=matching,
        resumed_from_job_id=job.id,
    )


def _auto_resume_count(session: Session, job: WorkerJob) -> int:
    """How many automatic restarts in a row led to ``job``."""
    count = 0
    while job is not None and job.requested_by == "auto_resume" and count <= AUTO_RESUME_MAX:
        count += 1
        job = session.get(WorkerJob, job.resumed_from_job_id) if job.resumed_from_job_id else None
    return count


def auto_resume_interrupted(
    factory: sessionmaker[Session], configs: dict[str, CompetitorConfig], matching: MatchingRuntime | None = None
) -> list[int]:
    """Restart the jobs interrupted by a worker restart (redeploy), when nothing else was
    started on their competitor since. Returns the ids of the new jobs."""
    created: list[int] = []
    with transaction(factory) as session:
        candidates = list(
            session.scalars(
                select(WorkerJob)
                .where(
                    WorkerJob.status == "interrupted",
                    WorkerJob.finished_at >= utcnow() - AUTO_RESUME_WINDOW,
                )
                .order_by(WorkerJob.finished_at)
            )
        )
        for job in candidates:
            later = session.scalar(
                select(exists().where(WorkerJob.competitor_id == job.competitor_id, WorkerJob.id > job.id))
            )
            if later:
                continue  # already restarted, or the user started something else
            competitor = session.get(Competitor, job.competitor_id)
            label = f"{TASK_LABELS[job.task]}: {competitor.name}"
            if _auto_resume_count(session, job) >= AUTO_RESUME_MAX:
                already = session.scalar(
                    select(
                        exists().where(Notification.job_id == job.id, Notification.kind == "job_auto_resume_stopped")
                    )
                )
                if not already:
                    notify(
                        session,
                        level="error",
                        kind="job_auto_resume_stopped",
                        title=f"{label} not resumed automatically",
                        message=f"interrupted {AUTO_RESUME_MAX} times in a row after automatic restarts: "
                        f"check the worker, then restart it with POST /api/v1/jobs/{job.id}/restart",
                        competitor_id=job.competitor_id,
                        job_id=job.id,
                    )
                continue
            config = configs.get(competitor.key)
            if config is None:
                continue
            try:
                new = restart_job(session, job, config, requested_by="auto_resume", matching=matching)
            except JobRequestError as exc:
                log.info("job #%s not resumed automatically: %s", job.id, exc.message)
                continue
            notify(
                session,
                level="info",
                kind="job_auto_resumed",
                title=f"{label} resumed automatically",
                message=f"job #{job.id} was interrupted by a worker restart; job #{new.id} continues it "
                "(work already done is skipped)",
                competitor_id=job.competitor_id,
                job_id=new.id,
                data={"resumed_from_job_id": job.id, "options": new.options},
            )
            created.append(new.id)
    return created


def request_cancel(session: Session, job: WorkerJob) -> WorkerJob:
    now = utcnow()
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = now
        job.cancel_requested_at = now
        job.step = "done"
        job.error = "cancelled before start"
        notify(
            session,
            level="info",
            kind="job_cancelled",
            title=f"{TASK_LABELS[job.task]} cancelled before start",
            competitor_id=job.competitor_id,
            job_id=job.id,
        )
    elif job.status in ("running", "paused"):
        if job.cancel_requested_at is None:
            job.cancel_requested_at = now
    else:
        raise JobRequestError("job_not_active", f"job #{job.id} is already {job.status}", job_id=job.id)
    session.flush()
    return job


def request_pause(session: Session, job: WorkerJob) -> WorkerJob:
    """The running job pauses after the current page and keeps its place (and the competitor's
    worker slot) until resumed. It does not survive a worker restart (it ends as interrupted)."""
    if job.status not in ("running", "paused"):
        raise JobRequestError(
            "job_not_running", f"job #{job.id} is {job.status}: only a running job can be paused", job_id=job.id
        )
    if job.cancel_requested_at is not None:
        raise JobRequestError("job_stopping", f"job #{job.id} is being cancelled", job_id=job.id)
    if job.pause_requested_at is None:
        job.pause_requested_at = utcnow()
    session.flush()
    return job


def request_resume(session: Session, job: WorkerJob) -> WorkerJob:
    if job.status not in ("running", "paused") or job.pause_requested_at is None:
        raise JobRequestError("job_not_paused", f"job #{job.id} is not paused", job_id=job.id)
    job.pause_requested_at = None
    session.flush()
    return job


def initial_progress(task: str, estimate: Estimate) -> dict[str, Any]:
    return {
        "percent": 0.0,
        "requests_done": 0,
        "requests_total": estimate.requests,
        "requests_total_is_estimate": True,
        "seconds_per_request": estimate.seconds_per_request,
        "elapsed_seconds": 0,
        "eta_seconds": estimate.seconds,
        "estimated_finish_at": None,
        "steps": [
            {"name": s, "label": STEP_LABELS[s], "status": "pending", "done": 0, "goal": None} for s in TASK_STEPS[task]
        ],
    }


# --------------------------------------------------------------------------- worker side


def claim_jobs(factory: sessionmaker[Session], instance: str, limit: int) -> list[int]:
    """Atomically move up to ``limit`` queued jobs to running for this worker instance."""
    if limit <= 0:
        return []
    with transaction(factory) as session:
        rows = session.execute(
            text(
                """
                UPDATE worker_jobs SET status = 'running', started_at = now(), heartbeat_at = now(),
                       claimed_by = :instance, step = 'starting'
                WHERE id IN (
                    SELECT id FROM worker_jobs WHERE status = 'queued'
                    ORDER BY created_at, id LIMIT :limit FOR UPDATE SKIP LOCKED
                )
                RETURNING id
                """
            ),
            {"instance": instance, "limit": limit},
        )
        return sorted(r[0] for r in rows)


def interrupt_orphan_jobs(
    factory: sessionmaker[Session], instance: str, stale_after: timedelta | None = None
) -> list[int]:
    """Close running/paused jobs whose worker is gone.

    ``stale_after=None`` (worker start-up): every running job of another instance; otherwise
    only those without heartbeat for ``stale_after``.
    """
    with transaction(factory) as session:
        stmt = select(WorkerJob).where(
            WorkerJob.status.in_(("running", "paused")), WorkerJob.claimed_by.is_distinct_from(instance)
        )
        if stale_after is not None:
            limit = utcnow() - stale_after
            stmt = stmt.where(
                text("coalesce(worker_jobs.heartbeat_at, worker_jobs.started_at) < :limit").bindparams(limit=limit)
            )
        jobs = list(session.scalars(stmt.with_for_update(skip_locked=True)))
        for job in jobs:
            job.status = "interrupted"
            job.finished_at = utcnow()
            job.step = "done"
            job.error = "the worker process stopped while the job was running (restart/redeploy)"
            if job.scrape_run_id:
                session.execute(
                    update(ScrapeRun)
                    .where(ScrapeRun.id == job.scrape_run_id, ScrapeRun.status == "running")
                    .values(status="aborted", finished_at=utcnow(), error="worker process stopped; closed by worker")
                )
            hint = (
                f" It is resumed automatically at worker start-up (unless WORKER_AUTO_RESUME=false), or with "
                f"POST /api/v1/jobs/{job.id}/restart: work already done is skipped."
            )
            notify(
                session,
                level="warning",
                kind="job_interrupted",
                title=f"{TASK_LABELS[job.task]} interrupted",
                message=job.error + "." + hint,
                competitor_id=job.competitor_id,
                job_id=job.id,
                run_id=job.scrape_run_id,
            )
        return [job.id for job in jobs]


class JobObserver(RunObserver):
    """Writes the progress of a running job to its row, reads cancel/pause requests.

    A pause happens at a safe point (``check_stop``, between pages): the thread waits there,
    with its whole crawl state in memory, until resumed, cancelled or shut down."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        job: WorkerJob,
        estimate: Estimate,
        stop_event: threading.Event,
        *,
        write_interval: float = 2.0,
        pause_poll_seconds: float = 2.0,
    ):
        self.factory = factory
        self.pause_poll_seconds = pause_poll_seconds
        self.job_id = job.id
        self.competitor_id = job.competitor_id
        self.task = job.task
        self.label = TASK_LABELS[job.task]
        self.estimate = estimate
        self.stop_event = stop_event
        self.write_interval = write_interval
        self.started = time.monotonic()
        self.run_id: int | None = None
        self.step = "starting"
        self.steps: dict[str, dict[str, Any]] = {
            s: {"name": s, "label": STEP_LABELS[s], "status": "pending", "done": 0, "goal": None, "pending": None}
            for s in TASK_STEPS[job.task]
        }
        self.counters: dict[str, Any] = {}
        self.listing_cap = (job.options or {}).get("max_listing_pages")
        self.cancel_requested = False
        self.pause_requested = False
        self.paused_seconds = 0.0
        self._paused_since: float | None = None
        self._last_write = 0.0
        # error tracking
        self.errors_total = 0
        self.error_types: Counter[str] = Counter()
        self.unreported_errors: list[dict[str, Any]] = []
        self.unreported_count = 0
        self.consecutive_failures = 0
        self._burst_notified = False
        self._last_digest = time.monotonic()

    # --------------------------------------------------------------- hooks
    def run_started(self, run_id: int) -> None:
        self.run_id = run_id
        with transaction(self.factory) as session:
            session.execute(update(WorkerJob).where(WorkerJob.id == self.job_id).values(scrape_run_id=run_id))

    def progress(self, step: str, done: int, goal: int | None = None, pending: int | None = None) -> None:
        changed = step != self.step
        if step in self.steps:
            for name, info in self.steps.items():  # steps before the current one are done
                if name == step:
                    break
                if info["status"] != "done":
                    info["status"] = "done"
                    info["goal"] = info["goal"] if info["goal"] is not None else info["done"]
            info = self.steps[step]
            info.update(status="running", done=done, goal=goal, pending=pending)
        self.step = step
        self._write(force=changed)

    def item(self, item_type: str, status: str, url: str, error: BaseException | None) -> None:
        if status not in FAILED_ITEM_STATUSES:
            if status != "skipped":
                self.consecutive_failures = 0
                self._burst_notified = False
            return
        self.errors_total += 1
        self.consecutive_failures += 1
        error_type = type(error).__name__ if error else status
        self.error_types[error_type] += 1
        self.unreported_count += 1
        if len(self.unreported_errors) < 50:
            self.unreported_errors.append(
                {
                    "url": url,
                    "item_type": item_type,
                    "status": status,
                    "error_type": error_type,
                    "message": str(error)[:500] if error else None,
                }
            )
        if self.consecutive_failures >= ERROR_BURST and not self._burst_notified:
            self._burst_notified = True
            self._notify(
                "error",
                "error_burst",
                f"{self.label}: {self.consecutive_failures} consecutive page failures",
                f"The last {self.consecutive_failures} pages failed (latest: {error_type} on {url}). "
                f"The job stops by itself after {MAX_CONSECUTIVE_FAILURES} consecutive failures.",
                {"last_error": str(error)[:500] if error else None, "url": url},
            )

    def checkpoint(self, counters: dict[str, int], credits_used: int) -> None:
        # Stored only: the services report progress right after each checkpoint, and that
        # call writes both (writing here would store the page count one page late).
        self.counters = {**counters, "credits_used": credits_used}

    def check_stop(self) -> None:
        self._raise_if_stopping()
        if self.pause_requested:
            self._pause()
            self._raise_if_stopping()
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise RunGuardStopped(f"{self.consecutive_failures} consecutive page failures")

    def _raise_if_stopping(self) -> None:
        if self.stop_event.is_set():
            raise RunInterrupted("worker shutting down")
        if self.cancel_requested:
            raise RunCancelled("cancelled by user")

    def _set_status(self, status: str, paused_at) -> None:
        with transaction(self.factory) as session:
            session.execute(
                update(WorkerJob)
                .where(WorkerJob.id == self.job_id, WorkerJob.status.in_(("running", "paused")))
                .values(status=status, paused_at=paused_at, heartbeat_at=utcnow())
            )

    def _pause(self) -> None:
        self._paused_since = time.monotonic()
        self._set_status("paused", utcnow())
        self._write(force=True)
        self._notify("info", "job_paused", f"{self.label} paused", "Paused after the current page; resume to continue.")
        log.info("job #%s paused", self.job_id)
        while self.pause_requested and not self.cancel_requested and not self.stop_event.is_set():
            self.stop_event.wait(self.pause_poll_seconds)
            self._write(force=True)  # heartbeat + reads the cancel/pause flags
        self.paused_seconds += time.monotonic() - self._paused_since
        self._paused_since = None
        if not self.stop_event.is_set():
            self._set_status("running", None)
            self._write(force=True)
        if not self.cancel_requested and not self.stop_event.is_set():
            self._notify("info", "job_resumed", f"{self.label} resumed", None)
            log.info("job #%s resumed", self.job_id)

    # --------------------------------------------------------------- progress
    def compute_progress(self, *, finished: bool = False) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started
        paused = self.paused_seconds + (time.monotonic() - self._paused_since if self._paused_since else 0.0)
        active = max(0.0, elapsed - paused)
        if finished:
            for info in self.steps.values():
                info["status"] = "done"
                info["goal"] = info["done"] if info["goal"] is None else info["goal"]
        is_estimate = False
        done_total = 0
        total: int | None = 0
        if self.task == "catalog_sync":
            listing = self.steps["crawl_listings"]
            products = self.steps["sync_products"]
            if listing["status"] == "done" or listing["goal"] is not None:
                listing_total = listing["goal"] if listing["goal"] is not None else listing["done"]
            else:
                is_estimate = True
                known = listing["done"] + (listing["pending"] or 0)
                listing_total = max(self.estimate.listing_pages or 0, known)
                if self.listing_cap:
                    listing_total = min(listing_total, int(self.listing_cap))
            if products["goal"] is not None:
                products_total = products["goal"]
            else:
                is_estimate = True
                products_total = self.estimate.products
            done_total = listing["done"] + products["done"]
            total = None if products_total is None else listing_total + products_total
        elif self.task == "matching":
            # units of work: texts embedded + products scored
            embed, match = self.steps["embed"], self.steps["match"]
            done_total = embed["done"] + match["done"]
            embed_total = embed["goal"] if embed["goal"] is not None else (self.estimate.embeddings or 0)
            match_total = match["goal"] if match["goal"] is not None else (self.estimate.products or 0)
            is_estimate = embed["goal"] is None or match["goal"] is None
            total = embed_total + match_total
        else:
            info = self.steps["refresh_prices"]
            done_total = info["done"]
            total = info["goal"] if info["goal"] is not None else self.estimate.requests
            is_estimate = info["goal"] is None
        measured = done_total >= 5
        spr = active / done_total if measured else self.estimate.seconds_per_request
        if finished:
            percent: float | None = 100.0
            eta: float | None = 0
        elif total:
            percent = round(min(99.9, 100.0 * done_total / total), 1)
            eta = max(0, total - done_total) * spr
        else:
            percent = 0.0 if total == 0 else None
            eta = None
        return {
            "percent": percent,
            "requests_done": done_total,
            "requests_total": total,
            "requests_total_is_estimate": is_estimate,
            "seconds_per_request": round(spr, 2),
            "seconds_per_request_measured": measured,
            "elapsed_seconds": round(elapsed),
            "paused_seconds": round(paused),
            "eta_seconds": round(eta) if eta is not None else None,
            "estimated_finish_at": (
                (utcnow() + timedelta(seconds=eta)).isoformat() if eta is not None and not self._paused_since else None
            ),
            "errors": self.errors_total,
            "steps": [{k: v for k, v in info.items()} for info in self.steps.values()],
        }

    def _write(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < self.write_interval:
            return
        self._last_write = now
        try:
            info = self.steps.get(self.step, {})
            with transaction(self.factory) as session:
                flags = session.execute(
                    update(WorkerJob)
                    .where(WorkerJob.id == self.job_id)
                    .values(
                        step=self.step,
                        step_done=info.get("done", 0),
                        step_goal=info.get("goal"),
                        progress=self.compute_progress(),
                        counters=self.counters,
                        heartbeat_at=utcnow(),
                    )
                    .returning(WorkerJob.cancel_requested_at, WorkerJob.pause_requested_at)
                ).one_or_none()
            if flags is not None:
                self.cancel_requested = flags[0] is not None
                self.pause_requested = flags[1] is not None
            if self.unreported_count and now - self._last_digest >= ERROR_DIGEST_SECONDS:
                self.flush_error_digest()
        except Exception:  # progress reporting must never kill the crawl
            log.exception("could not write the progress of job #%s", self.job_id)

    # --------------------------------------------------------------- notifications
    def flush_error_digest(self) -> None:
        if not self.unreported_count:
            return
        types = Counter(e["error_type"] for e in self.unreported_errors)
        summary = ", ".join(f"{n} {t}" for t, n in types.most_common())
        self._notify(
            "error",
            "page_errors",
            f"{self.label}: {self.unreported_count} new page error(s)",
            f"{summary}. Details: GET /api/v1/runs/{self.run_id}/items?status=failed",
            {"errors": self.unreported_errors, "count": self.unreported_count},
        )
        self.unreported_errors = []
        self.unreported_count = 0
        self._last_digest = time.monotonic()

    def _notify(
        self, level: str, kind: str, title: str, message: str | None, data: dict[str, Any] | None = None
    ) -> None:
        try:
            with transaction(self.factory) as session:
                notify(
                    session,
                    level=level,
                    kind=kind,
                    title=title,
                    message=message,
                    competitor_id=self.competitor_id,
                    job_id=self.job_id,
                    run_id=self.run_id,
                    data=data,
                )
        except Exception:
            log.exception("could not store notification %s", kind)


FetcherFactory = Callable[[CompetitorConfig], Fetcher]


def run_job(
    factory: sessionmaker[Session],
    job_id: int,
    configs: dict[str, CompetitorConfig],
    fetcher_factory: FetcherFactory,
    stop_event: threading.Event | None = None,
    *,
    write_interval: float = 2.0,
    pause_poll_seconds: float = 2.0,
    matching: MatchingRuntime | None = None,
) -> str:
    """Execute a claimed (running) job to the end; returns its final status. Never raises."""
    stop_event = stop_event or threading.Event()
    with transaction(factory) as session:
        job = session.get(WorkerJob, job_id)
        competitor = session.get(Competitor, job.competitor_id)
        key = competitor.key
        task, options = job.task, dict(job.options)
    observer: JobObserver | None = None
    status, error = "failed", None
    fetcher = None
    try:
        config = configs.get(key)
        if config is None:
            raise RuntimeError(f"no configuration file for competitor {key!r}")
        with transaction(factory) as session:
            estimate = estimate_job(
                session, job.competitor_id, config, task, options, matching_config=matching.config if matching else None
            )
        observer = JobObserver(
            factory, job, estimate, stop_event, write_interval=write_interval, pause_poll_seconds=pause_poll_seconds
        )
        _notify_tx(
            factory,
            level="info",
            kind="job_started",
            title=f"{TASK_LABELS[task]} started: {competitor.name}",
            message=_estimate_text(estimate),
            competitor_id=job.competitor_id,
            job_id=job_id,
            data={"estimate": estimate.as_dict()},
        )
        if task == "matching":
            if matching is None or not matching.configured:
                raise matching_service.MatchingError("matching_not_configured")
            result = MatchingRun(
                factory,
                job.competitor_id,
                matching,
                build_options(task, options),
                observer,
                job_id=job_id,
                competitor_options=config.matching,
            ).run()
            observer.counters = result.counters
        elif task == "catalog_sync":
            fetcher = fetcher_factory(config)
            result = CatalogSync(load_adapter(config), fetcher, factory, job.competitor_id, observer).run(
                build_options(task, options)
            )
        else:
            fetcher = fetcher_factory(config)
            observer.progress("select_products", 0)
            result = PriceRefresh(load_adapter(config), fetcher, factory, job.competitor_id, observer).run(
                build_options(task, options)
            )
        status = result.status
        if status == "failed" and task != "matching":
            with transaction(factory) as session:
                error = session.get(ScrapeRun, result.run_id).error
    except RunCancelled:
        status, error = "cancelled", "cancelled by user"
    except RunInterrupted:
        status, error = "interrupted", "worker shut down while the job was running (restart/redeploy)"
    except RunGuardStopped as exc:
        status, error = "failed", f"stopped by safety guard: {exc}"
    except matching_service.MatchingError as exc:
        status, error = "failed", f"{exc.code}: {exc.message}"
    except EmbeddingError as exc:  # Voyage refused (key, quota) or stayed unavailable
        status, error = "failed", f"embedding provider: {exc}"
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        log.exception("job #%s crashed", job_id)
        error_trace = traceback.format_exc(limit=8)
        _notify_tx(
            factory,
            level="error",
            kind="job_crashed",
            title=f"{TASK_LABELS[task]} crashed: {competitor.name}",
            message=error,
            competitor_id=job.competitor_id,
            job_id=job_id,
            run_id=observer.run_id if observer else None,
            data={"traceback": error_trace},
        )
    finally:
        close = getattr(fetcher, "close", None)
        if close:
            try:
                close()
            except Exception:
                log.exception("could not close the fetcher")
    try:
        finalize_job(factory, job_id, status, error, observer)
    except Exception:
        log.exception("could not finalize job #%s", job_id)
    if task in ("catalog_sync", "price_refresh") and status in ("succeeded", "partial"):
        detect_matching_changes(factory, job_id)
    return status


def detect_matching_changes(factory: sessionmaker[Session], job_id: int) -> None:
    """After a sync, the matches involving the synced products are checked: those whose
    competitor or reference product changed become deprecated (with a notification).
    Never fails the job."""
    try:
        with transaction(factory) as session:
            job = session.get(WorkerJob, job_id)
            deprecated = matching_service.detect_stale_matches(session, [job.competitor_id], job_id=job_id)
        if deprecated:
            log.info("job #%s: %d match(es) deprecated by the product changes", job_id, len(deprecated))
    except Exception:
        log.exception("could not check the matches after job #%s", job_id)


def finalize_job(
    factory: sessionmaker[Session], job_id: int, status: str, error: str | None, observer: JobObserver | None
) -> None:
    with transaction(factory) as session:
        job = session.get(WorkerJob, job_id)
        competitor = session.get(Competitor, job.competitor_id)
        run = session.get(ScrapeRun, job.scrape_run_id) if job.scrape_run_id else None
        job.status = status
        job.error = error
        job.paused_at = None
        job.finished_at = utcnow()
        job.step = "done"
        job.heartbeat_at = utcnow()
        if observer is not None:
            job.progress = observer.compute_progress(finished=status in ("succeeded", "partial"))
            job.counters = observer.counters
        if run is not None and status in ("succeeded", "partial"):
            if job.task == "catalog_sync" and run.full_scope:
                competitor.last_full_catalog_at = run.finished_at or utcnow()
                competitor.last_full_catalog_run_id = run.id
                competitor.last_full_catalog_status = status
            if job.task == "price_refresh":
                competitor.last_price_refresh_at = run.finished_at or utcnow()
        level = {"succeeded": "success", "partial": "warning", "cancelled": "info"}.get(status, "error")
        if status == "interrupted":
            level = "warning"
        data: dict[str, Any] = {"status": status, "counters": job.counters, "progress": job.progress}
        if observer is not None and observer.errors_total:
            data["error_types"] = dict(observer.error_types)
            data["unreported_errors"] = observer.unreported_errors
        if run is not None and run.stats.get("incomplete_reasons"):
            data["incomplete_reasons"] = run.stats["incomplete_reasons"][:20]
        notify(
            session,
            level=level,
            kind=f"job_{status}",
            title=f"{TASK_LABELS[job.task]} {status}: {competitor.name}",
            message=_summary(job, run, observer, error),
            competitor_id=job.competitor_id,
            job_id=job.id,
            run_id=run.id if run else None,
            data=data,
        )


def _summary(job: WorkerJob, run: ScrapeRun | None, observer: JobObserver | None, error: str | None) -> str:
    parts = []
    if job.started_at:
        parts.append(f"duration {_duration((job.finished_at - job.started_at).total_seconds())}")
    if run is not None and not run.full_scope and job.task == "catalog_sync":
        parts.append("limited scope (options), not a full catalog sync")
    if job.task == "matching" and observer is not None and observer.counters:
        c = observer.counters
        parts.append(
            f"{c.get('processed', 0)} products matched ({c.get('auto_matched', 0)} automatic, "
            f"{c.get('needs_review', 0)} to review, {c.get('no_match', 0)} without match), "
            f"{c.get('deprecated_detected', 0)} obsolete detected, {c.get('embedding_tokens', 0)} embedding tokens"
        )
    if run is not None:
        parts.append(
            f"{run.pages_fetched} pages, {run.categories_seen} categories, {run.products_seen} products "
            f"({run.products_created} new), {run.observations_created} price observations, "
            f"{run.pages_failed} failed pages, {run.credits_used} credits"
        )
    if observer is not None and observer.errors_total:
        parts.append("errors: " + ", ".join(f"{n} {t}" for t, n in observer.error_types.most_common()))
    if error:
        parts.append(f"error: {error}")
    return "; ".join(parts)


def _estimate_text(estimate: Estimate) -> str:
    if estimate.task == "matching":
        return f"{estimate.products} product(s) to match, ~{_duration(estimate.seconds or 0)}"
    if estimate.seconds is None:
        return "duration unknown (no history yet)"
    return f"about {estimate.requests} requests, ~{_duration(estimate.seconds)}, ~{estimate.credits} credits"


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return (
        f"{minutes // 60}h{minutes % 60:02d}" if minutes >= 60 else f"{minutes} min" if minutes else f"{seconds:.0f}s"
    )


def _notify_tx(factory: sessionmaker[Session], **kwargs: Any) -> None:
    try:
        with transaction(factory) as session:
            notify(session, **kwargs)
    except Exception:
        log.exception("could not store notification %s", kwargs.get("kind"))
