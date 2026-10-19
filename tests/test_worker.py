from datetime import UTC, datetime, timedelta

from competitor_catalog.services.worker import RunHistory, WorkerConfig, next_operation

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
CONFIG = WorkerConfig(
    competitors=["antalis_fr"],
    catalog_interval=timedelta(days=180),
    price_interval=timedelta(days=7),
    retry_after_failure=timedelta(hours=6),
    poll_seconds=600,
    stale_run_after=timedelta(minutes=30),
)


def history(catalog_done=None, catalog_attempt=None, price_done=None, price_attempt=None):
    return RunHistory(catalog_done, catalog_attempt, price_done, price_attempt)


def test_first_start_runs_the_catalog_sync():
    assert next_operation(history(), CONFIG, NOW) == "catalog_sync"


def test_catalog_every_180_days_prices_every_7_days():
    recent = NOW - timedelta(days=10)
    assert next_operation(history(recent, recent, NOW - timedelta(days=8)), CONFIG, NOW) == "price_refresh"
    assert next_operation(history(recent, recent, NOW - timedelta(days=2)), CONFIG, NOW) is None
    old = NOW - timedelta(days=181)
    assert next_operation(history(old, old, NOW - timedelta(days=1)), CONFIG, NOW) == "catalog_sync"


def test_failed_attempts_are_retried_after_a_delay():
    failed_start = NOW - timedelta(hours=1)
    assert next_operation(history(None, failed_start), CONFIG, NOW) is None
    assert next_operation(history(None, NOW - timedelta(hours=7)), CONFIG, NOW) == "catalog_sync"
    done = NOW - timedelta(days=30)
    assert next_operation(history(done, done, NOW - timedelta(days=8), NOW - timedelta(hours=2)), CONFIG, NOW) is None


def test_price_refresh_can_be_disabled_and_waits_for_a_catalog():
    disabled = WorkerConfig(**{**CONFIG.__dict__, "price_interval": None})
    recent = NOW - timedelta(days=10)
    assert next_operation(history(recent, recent, NOW - timedelta(days=30)), disabled, NOW) is None
    # no catalog yet and a recent failed attempt: no price refresh on an empty catalog
    assert next_operation(history(None, NOW - timedelta(hours=1)), CONFIG, NOW) is None
