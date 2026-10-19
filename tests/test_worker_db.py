from datetime import timedelta

import pytest

from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import ScrapeRun
from competitor_catalog.db.session import transaction
from competitor_catalog.services.runs import utcnow
from competitor_catalog.services.worker import abort_stale_runs, load_history
from tests.test_engine_db import TOY_CONFIG

pytestmark = pytest.mark.db


def test_history_ignores_aborted_runs_and_startup_closes_running_ones(factory):
    now = utcnow()
    with transaction(factory) as s:
        cid = repo.upsert_competitor(s, TOY_CONFIG).id
        s.add_all(
            [
                ScrapeRun(
                    competitor_id=cid,
                    operation="catalog_discovery",
                    status="aborted",
                    full_scope=True,
                    started_at=now - timedelta(hours=2),
                    finished_at=now - timedelta(hours=1),
                ),
                ScrapeRun(
                    competitor_id=cid,
                    operation="catalog_discovery",
                    status="failed",
                    full_scope=True,
                    started_at=now - timedelta(hours=5),
                    finished_at=now - timedelta(hours=5),
                ),
                ScrapeRun(
                    competitor_id=cid,
                    operation="catalog_discovery",
                    status="running",
                    full_scope=True,
                    started_at=now - timedelta(minutes=5),
                    heartbeat_at=now - timedelta(minutes=1),
                ),
            ]
        )
    with transaction(factory) as s:
        history = load_history(s, cid)
    assert history.last_full_catalog_done is None
    assert abs(history.last_catalog_attempt - (now - timedelta(hours=5))) < timedelta(seconds=1)  # failed only

    assert abort_stale_runs(factory, timedelta(minutes=30)) == 0  # heartbeat is fresh
    assert abort_stale_runs(factory, timedelta(0)) == 1  # worker start-up: closed
    with transaction(factory) as s:
        assert s.query(ScrapeRun).filter_by(status="running").count() == 0
