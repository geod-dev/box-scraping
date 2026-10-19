"""Long-running worker for server deployments (``catalog-scraper worker``).

A deliberately small scheduler: every ``poll`` interval it looks at the run history in
the database and decides, per competitor, whether a catalog sync or a price refresh is
due. Operations run one at a time in this process, so the configured crawl delay always
holds (never two crawlers on the same site) and a container restart simply resumes the
schedule from the database state.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.db.models import ScrapeRun
from competitor_catalog.db.session import transaction
from competitor_catalog.services.runs import utcnow

log = logging.getLogger(__name__)

DONE_STATUSES = ("succeeded", "partial")


@dataclass(frozen=True)
class WorkerConfig:
    competitors: list[str]
    catalog_interval: timedelta
    price_interval: timedelta | None  # None = price refresh disabled
    retry_after_failure: timedelta
    poll_seconds: float
    stale_run_after: timedelta
    resume_within: timedelta | None = None

    @classmethod
    def from_env(cls) -> WorkerConfig:
        price_hours = float(os.environ.get("PRICE_REFRESH_INTERVAL_HOURS", "168"))
        return cls(
            competitors=[c.strip() for c in os.environ.get("WORKER_COMPETITORS", "antalis_fr").split(",") if c.strip()],
            catalog_interval=timedelta(days=float(os.environ.get("CATALOG_SYNC_INTERVAL_DAYS", "180"))),
            price_interval=timedelta(hours=price_hours) if price_hours > 0 else None,
            retry_after_failure=timedelta(hours=float(os.environ.get("RUN_RETRY_AFTER_HOURS", "6"))),
            poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS", "600")),
            stale_run_after=timedelta(minutes=float(os.environ.get("STALE_RUN_MINUTES", "30"))),
            resume_within=timedelta(hours=float(os.environ.get("CATALOG_RESUME_HOURS", "48"))),
        )


@dataclass(frozen=True)
class RunHistory:
    last_full_catalog_done: datetime | None  # finished full-scope catalog run (succeeded/partial)
    last_catalog_attempt: datetime | None  # start of the last *failed* full catalog run
    last_price_capture: datetime | None  # finished run of either operation (succeeded/partial)
    last_price_attempt: datetime | None  # start of the last *failed* price refresh
    # (interrupted runs - status aborted - are resumed at once, without back-off)


def next_operation(history: RunHistory, config: WorkerConfig, now: datetime) -> str | None:
    """Pure decision function: ``"catalog_sync"``, ``"price_refresh"`` or ``None``."""

    def recently_attempted(attempt: datetime | None, done: datetime | None) -> bool:
        # an attempt newer than the last success that failed: wait before retrying
        return attempt is not None and (done is None or attempt > done) and now - attempt < config.retry_after_failure

    catalog_due = (
        history.last_full_catalog_done is None or now - history.last_full_catalog_done >= config.catalog_interval
    )
    if catalog_due and not recently_attempted(history.last_catalog_attempt, history.last_full_catalog_done):
        return "catalog_sync"
    if config.price_interval is not None and history.last_full_catalog_done is not None:
        price_due = history.last_price_capture is None or now - history.last_price_capture >= config.price_interval
        if price_due and not recently_attempted(history.last_price_attempt, history.last_price_capture):
            return "price_refresh"
    return None


def load_history(session: Session, competitor_id: int) -> RunHistory:
    def latest(column, *conditions):
        return session.scalar(select(func.max(column)).where(ScrapeRun.competitor_id == competitor_id, *conditions))

    return RunHistory(
        last_full_catalog_done=latest(
            ScrapeRun.finished_at,
            ScrapeRun.operation == "catalog_discovery",
            ScrapeRun.full_scope.is_(True),
            ScrapeRun.status.in_(DONE_STATUSES),
        ),
        last_catalog_attempt=latest(
            ScrapeRun.started_at,
            ScrapeRun.operation == "catalog_discovery",
            ScrapeRun.full_scope.is_(True),
            ScrapeRun.status == "failed",
        ),
        last_price_capture=latest(ScrapeRun.finished_at, ScrapeRun.status.in_(DONE_STATUSES)),
        last_price_attempt=latest(
            ScrapeRun.started_at, ScrapeRun.operation == "price_refresh", ScrapeRun.status == "failed"
        ),
    )


def abort_stale_runs(factory: sessionmaker[Session], older_than: timedelta) -> int:
    """Runs left 'running' by a killed process (container restart) are closed as aborted."""
    limit = utcnow() - older_than
    with transaction(factory) as session:
        result = session.execute(
            update(ScrapeRun)
            .where(ScrapeRun.status == "running", func.coalesce(ScrapeRun.heartbeat_at, ScrapeRun.started_at) < limit)
            .values(status="aborted", finished_at=utcnow(), error="process stopped (no heartbeat); closed by worker")
        )
        return result.rowcount or 0


def run_worker(settings, config: WorkerConfig, *, once: bool = False) -> None:  # pragma: no cover - I/O loop
    from competitor_catalog.app import build_context, build_fetcher
    from competitor_catalog.services.catalog_sync import CatalogSync, CatalogSyncOptions
    from competitor_catalog.services.price_refresh import PriceRefresh, PriceRefreshOptions

    log.info(
        "worker started: competitors=%s catalog every %s, prices every %s",
        config.competitors,
        config.catalog_interval,
        config.price_interval or "never",
    )
    first_pass = True
    while True:
        for key in config.competitors:
            ctx = build_context(settings, key)
            # At start-up every 'running' run belongs to a process that no longer exists
            # (redeploy/restart); afterwards only runs without recent heartbeat are closed.
            closed = abort_stale_runs(ctx.factory, timedelta(0) if first_pass else config.stale_run_after)
            if closed:
                log.warning("closed %d stale running run(s)", closed)
            with transaction(ctx.factory) as session:
                history = load_history(session, ctx.competitor_id)
            operation = next_operation(history, config, utcnow())
            if operation is None:
                continue
            log.info("%s: starting %s", key, operation)
            with build_fetcher(settings, ctx.config) as fetcher:
                if operation == "catalog_sync":
                    resume = config.resume_within.total_seconds() / 3600 if config.resume_within else None
                    result = CatalogSync(ctx.adapter, fetcher, ctx.factory, ctx.competitor_id).run(
                        CatalogSyncOptions(resume_within_hours=resume)
                    )
                else:
                    # products priced recently (e.g. by the catalog sync) are skipped
                    fresh_hours = config.price_interval.total_seconds() / 3600 / 2
                    result = PriceRefresh(ctx.adapter, fetcher, ctx.factory, ctx.competitor_id).run(
                        PriceRefreshOptions(skip_fresh_hours=fresh_hours)
                    )
            log.info("%s: %s run #%s finished with status %s", key, operation, result.run_id, result.status)
        first_pass = False
        if once:
            return
        time.sleep(config.poll_seconds)
