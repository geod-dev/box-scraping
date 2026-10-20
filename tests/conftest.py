from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.http.base import FetchedPage, TargetGoneError, TransientFetchError

FIXTURES = Path(__file__).parent / "fixtures"
DEFAULT_TEST_DB = "postgresql+psycopg://scraper:scraper@localhost:5433/competitor_catalog_test"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", help="run live Scrape.do tests (consumes credits)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live test: use --live (needs SCRAPEDO_API_KEY, consumes credits)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_page(text_: str, url: str = "https://www.antalis.fr/eshop/test", final_url: str | None = None) -> FetchedPage:
    return FetchedPage(url, final_url or url, 200, text_, datetime.now(UTC))


class FakeFetcher:
    """Serves pages from a dict; unknown URLs -> 404, listed URLs can fail transiently."""

    def __init__(self, pages: dict[str, str], *, failing: set[str] | None = None):
        self.pages = pages
        self.failing = failing or set()
        self.requests: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.requests.append(url)
        if url in self.failing:
            raise TransientFetchError("simulated proxy failure", url=url, status_code=502)
        if url not in self.pages:
            raise TargetGoneError("simulated 404", url=url, status_code=404)
        return FetchedPage(url, url, 200, self.pages[url], datetime.now(UTC), {"request_cost": 1})


# --------------------------------------------------------------------------- database


@pytest.fixture(scope="session")
def db_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB)


@pytest.fixture(scope="session")
def migrated_engine(db_url: str):
    from alembic import command
    from alembic.config import Config

    from competitor_catalog.db.session import make_engine
    from competitor_catalog.settings import PROJECT_ROOT

    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL test database not reachable ({db_url}): {exc.__class__.__name__}")
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    cfg.attributes["db_url"] = db_url
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def factory(migrated_engine) -> Iterator[sessionmaker[Session]]:
    from competitor_catalog.db.session import make_session_factory

    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE notifications, worker_jobs, service_heartbeats, scrape_observations, scrape_run_items, "
                "product_match_events, product_match_candidates, product_matches, product_embeddings, text_embeddings, "
                "products, categories, "
                "scrape_runs, competitors RESTART IDENTITY CASCADE"
            )
        )
    yield make_session_factory(migrated_engine)
