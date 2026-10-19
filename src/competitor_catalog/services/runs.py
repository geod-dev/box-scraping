"""Run tracking shared by both operations."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.db.models import ScrapeRun, ScrapeRunItem
from competitor_catalog.db.session import transaction

log = logging.getLogger(__name__)

COUNTER_COLUMNS = (
    "pages_fetched",
    "pages_failed",
    "categories_seen",
    "products_seen",
    "products_created",
    "products_updated",
    "products_failed",
    "products_without_price",
    "observations_created",
    "categories_marked_missing",
    "products_marked_missing",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class RunStopped(Exception):  # noqa: N818 - control flow, not an error
    """Raised at a safe point of a run when it must stop before the end."""


class RunCancelled(RunStopped):
    """A user asked to stop the run (API cancel)."""


class RunInterrupted(RunStopped):
    """The process is shutting down (redeploy, container stop)."""


class RunObserver:
    """Progress and cooperative-stop hooks of a run. The default implementation does nothing.

    ``progress`` reports the current step with the items done, the exact goal when known
    and, when the goal is unknown, the number of items already known to be pending.
    ``check_stop`` is called at safe points and may raise :class:`RunStopped`.
    """

    def run_started(self, run_id: int) -> None:
        pass

    def progress(self, step: str, done: int, goal: int | None = None, pending: int | None = None) -> None:
        pass

    def item(self, item_type: str, status: str, url: str, error: BaseException | None) -> None:
        pass

    def checkpoint(self, counters: dict[str, int], credits_used: int) -> None:
        pass

    def check_stop(self) -> None:
        pass


NULL_OBSERVER = RunObserver()


class RunTracker:
    def __init__(
        self,
        factory: sessionmaker[Session],
        competitor_id: int,
        operation: str,
        observer: RunObserver | None = None,
    ):
        self.factory = factory
        self.competitor_id = competitor_id
        self.operation = operation
        self.observer = observer or NULL_OBSERVER
        self.run_id: int | None = None
        self.counters: Counter[str] = Counter()
        self.stats: dict[str, Any] = {}
        self.credits_used = 0

    def start(self, params: dict[str, Any], *, full_scope: bool) -> int:
        with transaction(self.factory) as session:
            run = ScrapeRun(
                competitor_id=self.competitor_id,
                operation=self.operation,
                status="running",
                params=params,
                full_scope=full_scope,
                started_at=utcnow(),
                heartbeat_at=utcnow(),
            )
            session.add(run)
            session.flush()
            self.run_id = run.id
        log.info("started %s run #%s", self.operation, self.run_id)
        self.observer.run_started(self.run_id)
        return self.run_id

    def bump(self, **counts: int) -> None:
        for key, value in counts.items():
            if key not in COUNTER_COLUMNS:
                raise KeyError(key)
            self.counters[key] += value

    def snapshot(self) -> dict[str, int]:
        return {column: self.counters[column] for column in COUNTER_COLUMNS}

    def add_item(
        self,
        session: Session,
        *,
        item_type: str,
        url: str,
        status: str,
        product_id: int | None = None,
        category_id: int | None = None,
        error: BaseException | None = None,
        http_status: int | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        session.add(
            ScrapeRunItem(
                scrape_run_id=self.run_id,
                item_type=item_type,
                url=url,
                status=status,
                product_id=product_id,
                category_id=category_id,
                error_type=type(error).__name__ if error else None,
                error_message=str(error)[:2000] if error else None,
                http_status=http_status,
                warnings=warnings or None,
            )
        )
        self.observer.item(item_type, status, url, error)

    def add_item_tx(self, **kwargs: Any) -> None:
        with transaction(self.factory) as session:
            self.add_item(session, **kwargs)

    def flush(self, credits_used: int | None = None) -> None:
        if credits_used is not None:
            self.credits_used = credits_used
        with transaction(self.factory) as session:
            run = session.get(ScrapeRun, self.run_id)
            for column in COUNTER_COLUMNS:
                setattr(run, column, self.counters[column])
            run.credits_used = self.credits_used
            run.stats = self.stats
            run.heartbeat_at = utcnow()
        self.observer.checkpoint(self.snapshot(), self.credits_used)

    def finish(self, status: str, *, error: str | None = None, credits_used: int | None = None) -> None:
        self.flush(credits_used)
        with transaction(self.factory) as session:
            run = session.get(ScrapeRun, self.run_id)
            run.status = status
            run.error = error
            run.finished_at = utcnow()
        log.info("%s run #%s finished: %s %s", self.operation, self.run_id, status, dict(self.counters))
