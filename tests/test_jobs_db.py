"""Worker jobs: one-job-per-competitor rule, dispatcher end to end, cancel/shutdown, notifications."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import Competitor, Notification, ScrapeRun, WorkerJob
from competitor_catalog.db.session import transaction
from competitor_catalog.services import jobs
from competitor_catalog.services.dispatcher import Dispatcher, DispatcherConfig
from competitor_catalog.services.runs import utcnow
from tests.conftest import FakeFetcher
from tests.toy_adapter import BASE, toy_site

pytestmark = pytest.mark.db

TOY_YAML = {
    "key": "toy",
    "name": "Toy competitor",
    "base_url": BASE,
    "adapter": "tests.toy_adapter:ToyAdapter",
    "root_urls": [f"{BASE}/root"],
    "request_interval_seconds": 0,
}


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "toy.yaml").write_text(yaml.safe_dump(TOY_YAML), encoding="utf-8")
    return tmp_path


class Env:
    def __init__(self, factory, config_dir, fetcher_factory=None):
        self.factory = factory
        self.fetchers: list[FakeFetcher] = []
        self.site = toy_site()
        self.dispatcher = Dispatcher(
            factory,
            config_dir,
            fetcher_factory or self.make_fetcher,
            DispatcherConfig(poll_seconds=0, max_concurrent_jobs=4),
            write_interval=0,
            pause_poll_seconds=0.02,
        )
        self.dispatcher.startup()
        self.configs = self.dispatcher.configs

    def make_fetcher(self, config):
        fetcher = FakeFetcher(self.site)
        self.fetchers.append(fetcher)
        return fetcher

    def competitor(self) -> Competitor:
        with transaction(self.factory) as s:
            return repo.get_competitor(s, "toy")

    def enqueue(self, task, options=None) -> int:
        with transaction(self.factory) as s:
            return jobs.enqueue_job(s, repo.get_competitor(s, "toy"), self.configs["toy"], task, options).id

    def run_all(self) -> list[int]:
        started = self.dispatcher.tick()
        for thread in list(self.dispatcher.threads.values()):
            thread.join(30)
        self.dispatcher.tick()
        return started

    def job(self, job_id) -> WorkerJob:
        with transaction(self.factory) as s:
            return s.get(WorkerJob, job_id)

    def notifications(self) -> list[Notification]:
        with transaction(self.factory) as s:
            return list(s.scalars(select(Notification).order_by(Notification.id)))


@pytest.fixture
def env(factory, config_dir):
    return Env(factory, config_dir)


def test_nothing_runs_by_default(env):
    assert env.run_all() == []
    with transaction(env.factory) as s:
        assert s.query(WorkerJob).count() == 0
        assert s.query(ScrapeRun).count() == 0


def test_price_refresh_requires_a_full_catalog_sync_and_one_job_per_competitor(env):
    with pytest.raises(jobs.JobRequestError) as exc:
        env.enqueue("price_refresh")
    assert exc.value.code == "catalog_required"

    first = env.enqueue("catalog_sync")
    for task in ("catalog_sync", "price_refresh"):
        with pytest.raises(jobs.JobRequestError) as exc:
            env.enqueue(task)
        assert exc.value.code in ("worker_busy", "catalog_required")
    with pytest.raises(jobs.JobRequestError) as exc:
        env.enqueue("catalog_sync")
    assert exc.value.code == "worker_busy" and exc.value.job_id == first


def test_database_refuses_two_active_jobs_for_one_competitor(env):
    cid = env.competitor().id
    with pytest.raises(IntegrityError):
        with transaction(env.factory) as s:
            s.add_all(
                [
                    WorkerJob(competitor_id=cid, task="catalog_sync", status="queued"),
                    WorkerJob(competitor_id=cid, task="price_refresh", status="running"),
                ]
            )


def test_invalid_options_are_refused(env):
    with pytest.raises(jobs.JobRequestError) as exc:
        env.enqueue("catalog_sync", {"bogus": 1})
    assert exc.value.code == "invalid_options"


def test_catalog_sync_then_price_refresh_end_to_end(env):
    job_id = env.enqueue("catalog_sync")
    queued = env.job(job_id)
    assert queued.status == "queued"
    assert queued.estimate["task"] == "catalog_sync"
    assert queued.options["resume_within_hours"] == jobs.DEFAULT_CATALOG_RESUME_HOURS

    assert env.run_all() == [job_id]
    job = env.job(job_id)
    assert job.status == "succeeded", job.error
    assert job.scrape_run_id is not None
    assert job.progress["percent"] == 100.0
    assert job.progress["requests_done"] == 4 + 4  # 4 listing pages (cat A paginated) + 4 products
    assert all(step["status"] == "done" for step in job.progress["steps"])
    assert job.counters["products_seen"] == 4

    competitor = env.competitor()
    assert competitor.last_full_catalog_at is not None
    assert competitor.last_full_catalog_run_id == job.scrape_run_id
    assert competitor.last_full_catalog_status == "succeeded"
    kinds = [n.kind for n in env.notifications()]
    assert kinds == ["job_started", "job_succeeded"]

    # the catalog is known: the estimate now comes from the history
    with transaction(env.factory) as s:
        from competitor_catalog.services.estimates import estimate_job

        estimate = estimate_job(s, competitor.id, env.configs["toy"], "catalog_sync", {})
    assert estimate.listing_pages == 4 and estimate.products == 4 and estimate.requests == 8

    price_job = env.enqueue("price_refresh")
    env.run_all()
    job = env.job(price_job)
    assert job.status == "succeeded", job.error
    assert job.progress["requests_done"] == 4
    assert env.competitor().last_price_refresh_at is not None


def test_partial_scope_catalog_sync_does_not_count_as_full(env):
    job_id = env.enqueue("catalog_sync", {"max_products": 1})
    env.run_all()
    assert env.job(job_id).status == "partial"
    assert env.competitor().last_full_catalog_at is None
    assert env.notifications()[-1].kind == "job_partial"


class CancellingFetcher(FakeFetcher):
    """Simulates a user hitting 'stop' (or a container stop) after N requests."""

    def __init__(self, pages, after: int, action):
        super().__init__(pages)
        self.after = after
        self.action = action

    def fetch(self, url):
        if len(self.requests) == self.after:
            self.action()
        return super().fetch(url)


def test_cancel_running_job_stops_at_next_page(factory, config_dir):
    holder = {}

    def cancel():
        with transaction(factory) as s:
            jobs.request_cancel(s, s.get(WorkerJob, holder["job"]))

    env = Env(factory, config_dir, lambda config: CancellingFetcher(toy_site(), 2, cancel))
    holder["job"] = env.enqueue("catalog_sync")
    env.run_all()
    job = env.job(holder["job"])
    assert job.status == "cancelled"
    with transaction(factory) as s:
        run = s.get(ScrapeRun, job.scrape_run_id)
        assert run.status == "aborted"
        assert run.pages_fetched <= 4
    assert env.competitor().last_full_catalog_at is None
    assert env.notifications()[-1].kind == "job_cancelled"
    # the slot is free again
    assert env.enqueue("catalog_sync")


def test_cancel_queued_job_is_immediate(env):
    job_id = env.enqueue("catalog_sync")
    with transaction(env.factory) as s:
        jobs.request_cancel(s, s.get(WorkerJob, job_id))
    assert env.job(job_id).status == "cancelled"
    assert env.run_all() == []
    with pytest.raises(jobs.JobRequestError) as exc, transaction(env.factory) as s:
        jobs.request_cancel(s, s.get(WorkerJob, job_id))
    assert exc.value.code == "job_not_active"


def test_shutdown_interrupts_running_job(factory, config_dir):
    holder = {}
    env = Env(factory, config_dir, lambda config: CancellingFetcher(toy_site(), 1, holder["stop"].set))
    holder["stop"] = env.dispatcher.stop_event
    job_id = env.enqueue("catalog_sync")
    env.dispatcher.tick()
    for thread in list(env.dispatcher.threads.values()):
        thread.join(30)
    assert env.job(job_id).status == "interrupted"
    assert env.notifications()[-1].kind == "job_interrupted"


def test_startup_closes_jobs_left_running_by_a_dead_worker(env, factory, config_dir):
    job_id = env.enqueue("catalog_sync")
    assert jobs.claim_jobs(factory, "dead-worker", 5) == [job_id]
    assert jobs.claim_jobs(factory, "other", 5) == []  # claimed once only
    Env(factory, config_dir)  # a new worker process starts
    job = env.job(job_id)
    assert job.status == "interrupted"
    assert env.notifications()[-1].kind == "job_interrupted"


def test_stale_job_of_another_instance_is_closed(env, factory):
    job_id = env.enqueue("catalog_sync")
    jobs.claim_jobs(factory, "other-instance", 1)
    assert jobs.interrupt_orphan_jobs(factory, "me", timedelta(minutes=30)) == []  # fresh heartbeat
    with transaction(factory) as s:
        s.get(WorkerJob, job_id).heartbeat_at = utcnow() - timedelta(hours=1)
    assert jobs.interrupt_orphan_jobs(factory, "me", timedelta(minutes=30)) == [job_id]


def test_consecutive_failures_notify_and_stop_the_job(factory, config_dir, monkeypatch):
    monkeypatch.setattr(jobs, "ERROR_BURST", 2)
    monkeypatch.setattr(jobs, "MAX_CONSECUTIVE_FAILURES", 3)
    site = toy_site()
    failing = {url for url in site if "/p/" in url}
    env = Env(factory, config_dir, lambda config: FakeFetcher(site, failing=failing))
    job_id = env.enqueue("catalog_sync")
    env.run_all()
    job = env.job(job_id)
    assert job.status == "failed"
    assert "safety guard" in job.error
    notifications = env.notifications()
    kinds = [n.kind for n in notifications]
    assert "error_burst" in kinds and kinds[-1] == "job_failed"
    assert notifications[-1].level == "error"
    assert notifications[-1].data["error_types"] == {"TransientFetchError": 3}


def test_crash_in_a_job_is_reported(factory, config_dir):
    def broken(config):
        raise RuntimeError("proxy configuration broken")

    env = Env(factory, config_dir, broken)
    job_id = env.enqueue("catalog_sync")
    env.run_all()
    job = env.job(job_id)
    assert job.status == "failed" and "proxy configuration broken" in job.error
    kinds = [n.kind for n in env.notifications()]
    assert kinds[-2:] == ["job_crashed", "job_failed"]


def test_heartbeat_is_written(env):
    from competitor_catalog.db.models import ServiceHeartbeat

    with transaction(env.factory) as s:
        beat = s.get(ServiceHeartbeat, "worker")
    assert beat.instance == env.dispatcher.instance


def test_opt_in_scheduler_enqueues_the_first_catalog_sync(factory, config_dir):
    from competitor_catalog.services.worker import WorkerConfig

    env = Env(factory, config_dir)
    schedule = WorkerConfig(
        competitors=[],
        catalog_interval=timedelta(days=180),
        price_interval=timedelta(days=7),
        retry_after_failure=timedelta(hours=6),
        poll_seconds=0,
        stale_run_after=timedelta(minutes=30),
    )
    created = env.dispatcher.schedule_due_jobs(schedule)
    assert len(created) == 1 and env.job(created[0]).requested_by == "scheduler"
    assert env.dispatcher.schedule_due_jobs(schedule) == []  # busy
    with transaction(factory) as s:
        jobs.request_cancel(s, s.get(WorkerJob, created[0]))
    assert env.dispatcher.schedule_due_jobs(schedule) == []  # user cancelled: not restarted at once


def test_threads_are_isolated_per_competitor(factory, tmp_path):
    for key in ("toy", "toy2"):
        (tmp_path / f"{key}.yaml").write_text(yaml.safe_dump({**TOY_YAML, "key": key}), encoding="utf-8")
    seen = []
    lock = threading.Lock()

    def fetcher_factory(config):
        with lock:
            seen.append(config.key)
        return FakeFetcher(toy_site())

    env = Env(factory, tmp_path, fetcher_factory)
    with transaction(factory) as s:
        for key in ("toy", "toy2"):
            jobs.enqueue_job(s, repo.get_competitor(s, key), env.configs[key], "catalog_sync")
    assert len(env.run_all()) == 2
    assert sorted(seen) == ["toy", "toy2"]
    with transaction(factory) as s:
        assert {j.status for j in s.scalars(select(WorkerJob))} == {"succeeded"}


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def _pausing_env(factory, config_dir):
    holder = {}

    def pause():
        with transaction(factory) as s:
            jobs.request_pause(s, s.get(WorkerJob, holder["job"]))

    def make(config):
        holder["fetcher"] = CancellingFetcher(toy_site(), 3, pause)
        return holder["fetcher"]

    env = Env(factory, config_dir, make)
    holder["job"] = env.enqueue("catalog_sync")
    env.dispatcher.tick()
    wait_for(lambda: env.job(holder["job"]).status == "paused")
    return env, holder


def test_pause_then_resume_continues_where_it_stopped(factory, config_dir):
    env, holder = _pausing_env(factory, config_dir)
    job_id, fetcher = holder["job"], holder["fetcher"]
    paused = env.job(job_id)
    assert paused.paused_at is not None and paused.pause_requested_at is not None
    seen = len(fetcher.requests)
    time.sleep(0.2)
    assert len(fetcher.requests) == seen  # no request while paused
    with pytest.raises(jobs.JobRequestError) as exc:  # the slot stays taken
        env.enqueue("catalog_sync")
    assert exc.value.code == "worker_busy"

    with transaction(factory) as s:
        jobs.request_resume(s, s.get(WorkerJob, job_id))
    for thread in list(env.dispatcher.threads.values()):
        thread.join(30)
    job = env.job(job_id)
    assert job.status == "succeeded" and job.paused_at is None
    assert job.progress["paused_seconds"] >= 0
    assert len(fetcher.requests) == len(set(fetcher.requests)) == 8  # nothing fetched twice
    kinds = [n.kind for n in env.notifications()]
    assert kinds == ["job_started", "job_paused", "job_resumed", "job_succeeded"]


def test_cancel_a_paused_job(factory, config_dir):
    env, holder = _pausing_env(factory, config_dir)
    with transaction(factory) as s:
        jobs.request_cancel(s, s.get(WorkerJob, holder["job"]))
    for thread in list(env.dispatcher.threads.values()):
        thread.join(30)
    assert env.job(holder["job"]).status == "cancelled"
    assert [n.kind for n in env.notifications()][-1] == "job_cancelled"


def test_shutdown_while_paused_interrupts(factory, config_dir):
    env, holder = _pausing_env(factory, config_dir)
    env.dispatcher.shutdown()
    assert env.job(holder["job"]).status == "interrupted"


def test_pause_resume_refusals(env):
    job_id = env.enqueue("catalog_sync")
    with transaction(env.factory) as s:
        job = s.get(WorkerJob, job_id)
        with pytest.raises(jobs.JobRequestError) as exc:
            jobs.request_pause(s, job)
        assert exc.value.code == "job_not_running"
        with pytest.raises(jobs.JobRequestError) as exc:
            jobs.request_resume(s, job)
        assert exc.value.code == "job_not_paused"


def test_progress_written_after_checkpoint_is_not_one_page_late(env):
    """Services checkpoint (counters) then report progress: the stored step_done must be current."""
    from competitor_catalog.services.estimates import Estimate

    job_id = env.enqueue("catalog_sync")
    jobs.claim_jobs(env.factory, "me", 1)
    observer = jobs.JobObserver(
        env.factory,
        env.job(job_id),
        Estimate("catalog_sync", seconds_per_request=1),
        threading.Event(),
        write_interval=0.5,
    )
    observer.progress("crawl_listings", 0, None, pending=1)  # step change: written at once
    time.sleep(0.6)
    observer.checkpoint({"pages_fetched": 1}, 1)
    observer.progress("crawl_listings", 1, None, pending=3)
    job = env.job(job_id)
    assert job.step_done == 1 and job.counters["pages_fetched"] == 1
    assert job.progress["requests_done"] == 1
