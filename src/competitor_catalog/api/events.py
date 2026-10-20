"""Server-Sent Events for the console: ``GET /api/v1/events``.

The worker is another process that only writes to the database, so each API process runs
one poller (only while at least one client is connected): every ``poll_seconds`` it reads
the state the console shows, compares it with the previous snapshot and broadcasts the
differences to every open stream. Polling at 1 s also throttles ``job.updated`` to about
one event per second per job.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from competitor_catalog.api import schemas, views
from competitor_catalog.api.deps import require_api_key
from competitor_catalog.api.routes.matching import summary_out
from competitor_catalog.db.models import JOB_ACTIVE_STATUSES, Competitor, Notification, ProductMatch, WorkerJob

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
RETRY_MS = 3000
QUEUE_SIZE = 500  # events buffered per client; a client that falls further behind is disconnected
NOTIFICATIONS_PER_POLL = 200

router = APIRouter(tags=["events"])


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n"


@dataclass
class Snapshot:
    jobs: dict[int, dict[str, Any]]  # job id -> JobOut of the active jobs
    observed: dict[int, dict[str, Any]]  # the same plus the jobs that just finished or were just created
    max_job_id: int
    max_notification_id: int
    competitors: dict[int, tuple]  # competitor id -> fingerprint (worker state, sync timestamps)
    matching: dict[int, tuple]  # competitor id -> (counts per status, last computation)
    summary: dict[str, Any]
    new_notifications: list[Notification] = field(default_factory=list)


class EventHub:
    def __init__(
        self, app: FastAPI, *, poll_seconds: float = POLL_SECONDS, heartbeat_seconds: float = HEARTBEAT_SECONDS
    ):
        self.app = app
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.summary: dict[str, Any] | None = None  # latest console.summary payload
        self._state: Snapshot | None = None
        self._subscribers: set[asyncio.Queue[str | None]] = set()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ subscribers

    async def subscribe(self) -> asyncio.Queue[str | None]:
        async with self._lock:
            if self._task is None or self._task.done():
                await asyncio.to_thread(self.poll)  # fresh baseline: no diff events
                self._task = asyncio.create_task(self._run(), name="sse-poller")
            queue: asyncio.Queue[str | None] = asyncio.Queue(QUEUE_SIZE)
            self._subscribers.add(queue)
            return queue

    def unsubscribe(self, queue: asyncio.Queue[str | None]) -> None:
        self._subscribers.discard(queue)

    def broadcast(self, message: str) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:  # too slow: end its stream, the client reconnects
                self._subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while self._subscribers:
            await asyncio.sleep(self.poll_seconds)
            try:
                messages = await asyncio.to_thread(self.poll)
            except Exception:
                log.exception("SSE poll failed")
                continue
            for message in messages:
                self.broadcast(message)

    # ------------------------------------------------------------------ polling

    def poll(self) -> list[str]:
        """Reads the state, returns the SSE messages of what changed since the previous call
        (nothing on the first call)."""
        with self.app.state.factory() as session:
            previous = self._state
            current = self._snapshot(session, previous)
            messages = self._diff(session, previous, current) if previous is not None else []
        self._state = current
        self.summary = current.summary
        return messages

    def _snapshot(self, session: Session, previous: Snapshot | None) -> Snapshot:
        watched = WorkerJob.status.in_(JOB_ACTIVE_STATUSES)
        if previous is None:
            known_job_id = session.scalar(select(func.max(WorkerJob.id))) or 0
        else:  # also the jobs that were active, and the new ones (even finished already)
            known_job_id = previous.max_job_id
            watched |= WorkerJob.id.in_(list(previous.jobs)) | (WorkerJob.id > known_job_id)
        jobs = list(
            session.scalars(select(WorkerJob).where(watched).order_by(WorkerJob.created_at.desc(), WorkerJob.id.desc()))
        )
        outs = views.jobs_out(session, jobs)
        job_outs = {job.id: out.model_dump(mode="json") for job, out in zip(jobs, outs, strict=True)}
        active = {job.competitor_id: job for job in jobs if job.status in JOB_ACTIVE_STATUSES}
        max_job_id = max([known_job_id, *job_outs])

        new_notifications: list[Notification] = []
        if previous is None:
            max_notification_id = session.scalar(select(func.max(Notification.id))) or 0
        else:
            new_notifications = list(
                session.scalars(
                    select(Notification)
                    .where(Notification.id > previous.max_notification_id)
                    .order_by(Notification.id)
                    .limit(NOTIFICATIONS_PER_POLL)
                )
            )
            max_notification_id = max([previous.max_notification_id, *(n.id for n in new_notifications)])
        by_level = dict(
            session.execute(
                select(Notification.level, func.count())
                .where(Notification.read_at.is_(None))
                .group_by(Notification.level)
            ).all()
        )

        competitors = list(session.scalars(select(Competitor).order_by(Competitor.name)))
        fingerprints = {
            c.id: (
                views.worker_state(active.get(c.id)),
                active[c.id].id if c.id in active else None,
                c.enabled,
                c.last_full_catalog_at,
                c.last_full_catalog_run_id,
                c.last_full_catalog_status,
                c.last_price_refresh_at,
            )
            for c in competitors
        }

        reference = self.app.state.matching.config.reference_competitor
        counts: dict[int, dict[str, int]] = defaultdict(dict)
        for competitor_id, status, n in session.execute(
            select(ProductMatch.competitor_id, ProductMatch.status, func.count()).group_by(
                ProductMatch.competitor_id, ProductMatch.status
            )
        ).all():
            counts[competitor_id][status] = n
        computed = dict(
            session.execute(
                select(ProductMatch.competitor_id, func.max(ProductMatch.computed_at)).group_by(
                    ProductMatch.competitor_id
                )
            ).all()
        )
        matched = [c for c in competitors if c.key != reference]
        matching = {c.id: (sorted(counts[c.id].items()), computed.get(c.id)) for c in matched}

        summary = schemas.ConsoleSummary(
            jobs=[job_outs[job.id] for job in jobs if job.status in JOB_ACTIVE_STATUSES],
            competitors=[
                schemas.ConsoleCompetitor(
                    key=c.key,
                    name=c.name,
                    enabled=c.enabled,
                    worker_state=fingerprints[c.id][0],
                    job_id=fingerprints[c.id][1],
                )
                for c in competitors
            ],
            matching=schemas.MatchingTotals(
                needs_review=sum(counts[c.id].get("needs_review", 0) for c in matched),
                deprecated=sum(counts[c.id].get("deprecated", 0) for c in matched),
            ),
            notifications=schemas.UnreadCount(unread=sum(by_level.values()), by_level=by_level),
        ).model_dump(mode="json")
        return Snapshot(
            jobs={job.id: job_outs[job.id] for job in jobs if job.status in JOB_ACTIVE_STATUSES},
            observed=job_outs,
            max_job_id=max_job_id,
            max_notification_id=max_notification_id,
            competitors=fingerprints,
            matching=matching,
            summary=summary,
            new_notifications=new_notifications,
        )

    def _diff(self, session: Session, previous: Snapshot, current: Snapshot) -> list[str]:
        messages = []
        for job_id, job in sorted(current.observed.items()):
            if previous.jobs.get(job_id) != job:
                messages.append(sse("job.updated", job))
            if job["status"] not in JOB_ACTIVE_STATUSES:
                messages.append(sse("job.finished", job))
        for notification in current.new_notifications:
            messages.append(sse("notification.created", views.notification_out(notification).model_dump(mode="json")))
        matching_runtime = self.app.state.matching
        for competitor_id, fingerprint in current.competitors.items():
            if previous.competitors.get(competitor_id) != fingerprint:
                competitor = session.get(Competitor, competitor_id)
                data = schemas.CompetitorOut.model_validate(views.competitor_out(session, competitor, matching_runtime))
                messages.append(sse("competitor.updated", data.model_dump(mode="json")))
        for competitor_id, fingerprint in current.matching.items():
            if previous.matching.get(competitor_id, ([], None)) != fingerprint:
                competitor = session.get(Competitor, competitor_id)
                data = summary_out(session, competitor, matching_runtime)
                messages.append(sse("matching.updated", data.model_dump(mode="json")))
        if current.summary != previous.summary:
            messages.append(sse("console.summary", current.summary))
        return messages


# --------------------------------------------------------------------------- endpoint

EVENTS_DESCRIPTION = """\
Server-Sent Events (`text/event-stream`) for the console. Named events, JSON in `data:`:

* `console.summary` (ConsoleSummary): sent on connect, then whenever any part changes.
* `job.updated` (Job): any change of an active job (at most ~1/s per job), and job creation.
* `job.finished` (Job): the job reached succeeded, partial, failed, cancelled or interrupted.
* `notification.created` (Notification).
* `matching.updated` (MatchingSummary): a competitor whose match counts changed.
* `competitor.updated` (Competitor): worker state or sync timestamps changed.

Comment heartbeat `: ping` every 15 s, `retry: 3000`. Changes are detected by polling the
database every second: an event arrives up to ~1 s after the change. A client that cannot
keep up is disconnected (it reconnects and gets a fresh `console.summary`)."""


@router.get(
    "/events",
    summary="Live console events (Server-Sent Events)",
    description=EVENTS_DESCRIPTION,
    dependencies=[Depends(require_api_key)],
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}, "description": "event stream"}},
)
async def events(request: Request) -> StreamingResponse:
    hub: EventHub = request.app.state.events
    queue = await hub.subscribe()
    summary = hub.summary

    async def stream():
        try:
            yield f"retry: {RETRY_MS}\n\n"
            yield sse("console.summary", summary)
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), hub.heartbeat_seconds)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if message is None:
                    return
                yield message
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
