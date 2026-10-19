"""Worker jobs: one task (catalog sync or price refresh) for one competitor.

Lifecycle: ``queued`` (created by the API) -> ``running`` (claimed by the worker process)
-> ``succeeded`` | ``partial`` | ``failed`` | ``cancelled`` | ``interrupted``.

Rules enforced here and by the database (partial unique index on ``worker_jobs``):

* a competitor has at most one queued/running job, whatever its task;
* a price refresh needs a previous full catalog sync (``competitors.last_full_catalog_at``);
* nothing runs unless a job was requested (or the opt-in scheduler is enabled).
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

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.core.adapter import load_adapter
from competitor_catalog.core.config import CompetitorConfig
from competitor_catalog.db.models import JOB_ACTIVE_STATUSES, Competitor, ScrapeRun, WorkerJob
from competitor_catalog.db.session import transaction
from competitor_catalog.http.base import Fetcher
from competitor_catalog.services.catalog_sync import CatalogSync, CatalogSyncOptions
from competitor_catalog.services.estimates import Estimate, estimate_job
from competitor_catalog.services.notifications import notify
from competitor_catalog.services.price_refresh import PriceRefresh, PriceRefreshOptions
from competitor_catalog.services.runs import RunCancelled, RunInterrupted, RunObserver, RunStopped, utcnow

log = logging.getLogger(__name__)

TASKS = ("catalog_sync", "price_refresh")
TASK_LABELS = {"catalog_sync": "Catalog sync", "price_refresh": "Price refresh"}
STEP_LABELS = {
    "queued": "Waiting for the worker",
    "starting": "Starting",
    "crawl_listings": "Crawling category/listing pages",
    "sync_products": "Syncing product pages (details + prices)",
    "select_products": "Selecting products",
    "refresh_prices": "Refreshing product prices",
    "finalize": "Finalizing",
    "done": "Done",
}
TASK_STEPS = {
    "catalog_sync": ("crawl_listings", "sync_products", "finalize"),
    "price_refresh": ("refresh_prices",),
}
# Default for catalog syncs started by the API: products whose details were synced less
# than 48 h ago (e.g. by an interrupted previous run) are confirmed without being refetched.
DEFAULT_CATALOG_RESUME_HOURS = 48.0
FAILED_ITEM_STATUSES = ("failed", "not_found")
ERROR_BURST = 10  # consecutive failed pages -> immediate error notification
MAX_CONSECUTIVE_FAILURES = 50  # -> the job stops (site blocking us, proxy outage...)
ERROR_DIGEST_SECONDS = 900  # page errors are notified in digests at most every 15 min


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
    cls = CatalogSyncOptions if task == "catalog_sync" else PriceRefreshOptions
    allowed = {f.name for f in fields(cls)}
    unknown = set(options) - allowed
    if unknown:
        raise JobRequestError("invalid_options", f"unknown options for {task}: {sorted(unknown)}")
    if task == "catalog_sync":
        options.setdefault("resume_within_hours", DEFAULT_CATALOG_RESUME_HOURS)
    try:
        return asdict(cls(**options))
    except TypeError as exc:
        raise JobRequestError("invalid_options", str(exc)) from exc


def build_options(task: str, options: dict[str, Any]) -> CatalogSyncOptions | PriceRefreshOptions:
    cls = CatalogSyncOptions if task == "catalog_sync" else PriceRefreshOptions
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in options.items() if k in allowed})


def is_full_scope(task: str, options: dict[str, Any]) -> bool:
    return task == "catalog_sync" and build_options(task, options).is_full_scope


# --------------------------------------------------------------------------- API side


def active_job(session: Session, competitor_id: int) -> WorkerJob | None:
    return session.scalar(
        select(WorkerJob).where(WorkerJob.competitor_id == competitor_id, WorkerJob.status.in_(JOB_ACTIVE_STATUSES))
    )


def allowed_tasks(session: Session, competitor: Competitor) -> dict[str, str | None]:
    """task -> None when it can be started now, else the refusal code."""
    busy = active_job(session, competitor.id) is not None
    result: dict[str, str | None] = {}
    for task in TASKS:
        if not competitor.enabled:
            result[task] = "competitor_disabled"
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
) -> WorkerJob:
    options = normalize_options(task, options)
    if not competitor.enabled:
        raise JobRequestError("competitor_disabled", f"competitor {competitor.key} is disabled in its config")
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
    estimate = estimate_job(session, competitor.id, config, task, options)
    job = WorkerJob(
        competitor_id=competitor.id,
        task=task,
        status="queued",
        options=options,
        requested_by=requested_by,
        step="queued",
        estimate=estimate.as_dict(),
        progress=initial_progress(task, estimate),
    )
    try:
        with session.begin_nested():  # the unique index settles concurrent requests
            session.add(job)
            session.flush()
    except IntegrityError:
        raise JobRequestError("worker_busy", f"{competitor.key} already has an active job") from None
    return job


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
                " Start a new catalog sync to resume: products synced in the last 48 h are not fetched again."
                if job.task == "catalog_sync"
                else ""
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
        adapter = load_adapter(config)
        with transaction(factory) as session:
            estimate = estimate_job(session, job.competitor_id, config, task, options)
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
        fetcher = fetcher_factory(config)
        if task == "catalog_sync":
            result = CatalogSync(adapter, fetcher, factory, job.competitor_id, observer).run(
                build_options(task, options)
            )
        else:
            observer.progress("select_products", 0)
            result = PriceRefresh(adapter, fetcher, factory, job.competitor_id, observer).run(
                build_options(task, options)
            )
        status = result.status
        if status == "failed":
            with transaction(factory) as session:
                error = session.get(ScrapeRun, result.run_id).error
    except RunCancelled:
        status, error = "cancelled", "cancelled by user"
    except RunInterrupted:
        status, error = "interrupted", "worker shut down while the job was running (restart/redeploy)"
    except RunGuardStopped as exc:
        status, error = "failed", f"stopped by safety guard: {exc}"
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
    return status


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
