"""The worker process (``catalog-scraper worker``): executes the jobs requested through the API.

* polls ``worker_jobs`` for queued jobs and claims them atomically (``SKIP LOCKED``);
* each job runs in its own thread with its own fetcher and rate limiter; the database
  guarantees at most one active job per competitor, so a site is never crawled twice
  in parallel, while different competitors can run at the same time;
* writes a heartbeat (``service_heartbeats``) so the API can tell whether the worker is up;
* on SIGTERM/SIGINT (redeploy, ``docker stop``) running jobs stop at the next page and are
  closed as ``interrupted``; at start-up, jobs left running by a killed process are closed
  the same way. Nothing is restarted automatically: by default nothing runs unless asked.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.core.config import CompetitorConfig, load_competitor_configs
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import ServiceHeartbeat, WorkerJob
from competitor_catalog.db.session import transaction
from competitor_catalog.services import jobs
from competitor_catalog.services.runs import utcnow
from competitor_catalog.services.worker import WorkerConfig, abort_stale_runs, load_history, next_operation

log = logging.getLogger(__name__)

HEARTBEAT_NAME = "worker"


@dataclass(frozen=True)
class DispatcherConfig:
    poll_seconds: float = 3.0
    max_concurrent_jobs: int = 4
    stale_job_after: timedelta = timedelta(minutes=30)
    shutdown_grace_seconds: float = 60.0
    auto_schedule: WorkerConfig | None = None  # None = nothing runs unless requested

    @classmethod
    def from_env(cls) -> DispatcherConfig:
        auto = os.environ.get("AUTO_SCHEDULE", "false").strip().lower() in ("1", "true", "yes", "on")
        return cls(
            poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", "3")),
            max_concurrent_jobs=int(os.environ.get("WORKER_MAX_CONCURRENT_JOBS", "4")),
            stale_job_after=timedelta(minutes=float(os.environ.get("STALE_JOB_MINUTES", "30"))),
            shutdown_grace_seconds=float(os.environ.get("WORKER_SHUTDOWN_GRACE_SECONDS", "60")),
            auto_schedule=WorkerConfig.from_env() if auto else None,
        )


class Dispatcher:
    def __init__(
        self,
        factory: sessionmaker[Session],
        config_dir,
        fetcher_factory: jobs.FetcherFactory,
        config: DispatcherConfig | None = None,
        *,
        write_interval: float = 2.0,
        pause_poll_seconds: float = 2.0,
    ):
        self.pause_poll_seconds = pause_poll_seconds
        self.factory = factory
        self.config_dir = config_dir
        self.fetcher_factory = fetcher_factory
        self.config = config or DispatcherConfig()
        self.write_interval = write_interval
        self.instance = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stop_event = threading.Event()
        self.threads: dict[int, threading.Thread] = {}
        self.configs: dict[str, CompetitorConfig] = {}
        self.started_at = utcnow()

    # ------------------------------------------------------------------ lifecycle
    def startup(self) -> None:
        self.reload_configs()
        closed = jobs.interrupt_orphan_jobs(self.factory, self.instance, stale_after=None)
        if closed:
            log.warning("closed %d job(s) left running by a previous worker process: %s", len(closed), closed)
        runs = abort_stale_runs(self.factory, timedelta(0))
        if runs:
            log.warning("closed %d scrape run(s) left running", runs)
        self.heartbeat()
        log.info(
            "worker %s ready: max %d concurrent jobs, auto schedule %s",
            self.instance,
            self.config.max_concurrent_jobs,
            "ON" if self.config.auto_schedule else "off",
        )

    def reload_configs(self) -> None:
        self.configs = load_competitor_configs(self.config_dir)
        with transaction(self.factory) as session:
            for config in self.configs.values():
                repo.upsert_competitor(session, config)

    def tick(self) -> list[int]:
        """One scheduling pass; returns the ids of the jobs started."""
        for job_id, thread in list(self.threads.items()):
            if not thread.is_alive():
                thread.join()
                del self.threads[job_id]
        self.heartbeat()
        stale = jobs.interrupt_orphan_jobs(self.factory, self.instance, stale_after=self.config.stale_job_after)
        if stale:
            log.warning("closed %d stale job(s) of another worker instance: %s", len(stale), stale)
        if self.stop_event.is_set():
            return []
        if self.config.auto_schedule is not None:
            self.schedule_due_jobs(self.config.auto_schedule)
        started = jobs.claim_jobs(self.factory, self.instance, self.config.max_concurrent_jobs - len(self.threads))
        for job_id in started:
            thread = threading.Thread(target=self._run, args=(job_id,), name=f"job-{job_id}", daemon=True)
            self.threads[job_id] = thread
            thread.start()
            log.info("started job #%s", job_id)
        return started

    def _run(self, job_id: int) -> None:
        status = jobs.run_job(
            self.factory,
            job_id,
            self.configs,
            self.fetcher_factory,
            self.stop_event,
            write_interval=self.write_interval,
            pause_poll_seconds=self.pause_poll_seconds,
        )
        log.info("job #%s finished: %s", job_id, status)

    def heartbeat(self) -> None:
        now = utcnow()
        details = {"running_jobs": sorted(self.threads), "auto_schedule": self.config.auto_schedule is not None}
        with transaction(self.factory) as session:
            session.execute(
                insert(ServiceHeartbeat)
                .values(
                    name=HEARTBEAT_NAME,
                    instance=self.instance,
                    started_at=self.started_at,
                    heartbeat_at=now,
                    details=details,
                )
                .on_conflict_do_update(
                    index_elements=["name"],
                    set_={
                        "instance": self.instance,
                        "started_at": self.started_at,
                        "heartbeat_at": now,
                        "details": details,
                    },
                )
            )

    def run_forever(self) -> None:  # pragma: no cover - I/O loop
        def stop(signum, _frame):
            log.warning("signal %s received: stopping running jobs at the next page", signum)
            self.stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.startup()
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("worker tick failed")
            self.stop_event.wait(self.config.poll_seconds)
        self.shutdown()

    def shutdown(self) -> None:
        self.stop_event.set()
        for job_id, thread in list(self.threads.items()):
            thread.join(self.config.shutdown_grace_seconds)
            if thread.is_alive():
                log.error("job #%s did not stop within the grace period", job_id)
        log.info("worker stopped")

    # ------------------------------------------------------------------ opt-in schedule
    def schedule_due_jobs(self, schedule: WorkerConfig) -> list[int]:
        created = []
        now = utcnow()
        for key, config in self.configs.items():
            if not config.enabled or (schedule.competitors and key not in schedule.competitors):
                continue
            with transaction(self.factory) as session:
                competitor = repo.get_competitor(session, key)
                if competitor is None or jobs.active_job(session, competitor.id) is not None:
                    continue
                last = session.scalar(
                    select(WorkerJob)
                    .where(WorkerJob.competitor_id == competitor.id)
                    .order_by(WorkerJob.created_at.desc())
                    .limit(1)
                )
                if (
                    last is not None
                    and last.status in ("cancelled", "failed")
                    and last.finished_at
                    and now - last.finished_at < schedule.retry_after_failure
                ):
                    continue  # a user stopped it, or it failed: do not restart it at once
                operation = next_operation(load_history(session, competitor.id), schedule, now)
                if operation is None:
                    continue
                options = {}
                if operation == "price_refresh" and schedule.price_interval:
                    options["skip_fresh_hours"] = schedule.price_interval.total_seconds() / 3600 / 2
                try:
                    job = jobs.enqueue_job(session, competitor, config, operation, options, requested_by="scheduler")
                except jobs.JobRequestError as exc:
                    log.info("scheduler: %s %s not enqueued: %s", key, operation, exc.message)
                    continue
                created.append(job.id)
                log.info("scheduler: enqueued %s for %s (job #%s)", operation, key, job.id)
        return created
