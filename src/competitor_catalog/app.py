"""Composition root: builds adapter, fetcher, DB session factory for one competitor."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.core.adapter import CompetitorAdapter, load_adapter
from competitor_catalog.core.config import CompetitorConfig, load_competitor_config
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.session import make_engine, make_session_factory, transaction
from competitor_catalog.http.ratelimit import MinIntervalRateLimiter
from competitor_catalog.http.scrapedo import ScrapeDoClient, ScrapeDoOptions
from competitor_catalog.matching.config import load_matching_config
from competitor_catalog.matching.embeddings import VoyageEmbedder
from competitor_catalog.matching.service import MatchingRuntime
from competitor_catalog.settings import Settings


@dataclass
class CompetitorContext:
    settings: Settings
    config: CompetitorConfig
    adapter: CompetitorAdapter
    factory: sessionmaker[Session]
    competitor_id: int


def build_fetcher(settings: Settings, config: CompetitorConfig, *, interval: float | None = None) -> ScrapeDoClient:
    return ScrapeDoClient(
        settings.require_scrapedo_api_key(),
        options=ScrapeDoOptions.from_mapping(config.scrapedo),
        rate_limiter=MinIntervalRateLimiter(config.request_interval_seconds if interval is None else interval),
        max_attempts=config.max_attempts,
        archive_dir=settings.html_archive_dir,
    )


def build_matching_runtime(settings: Settings) -> MatchingRuntime:
    """Matching config; embeddings only when VOYAGE_API_KEY is set (else matching is disabled)."""
    config = load_matching_config(settings.matching_config_path)
    api_key = settings.voyage_api_key
    factory = (lambda: VoyageEmbedder(api_key, config.embedding)) if api_key else None
    return MatchingRuntime(config, factory)


def session_factory(settings: Settings) -> sessionmaker[Session]:
    return make_session_factory(make_engine(settings.require_database_url()))


def build_context(settings: Settings, competitor_key: str) -> CompetitorContext:
    config = load_competitor_config(settings.competitor_config_dir, competitor_key)
    adapter = load_adapter(config)
    factory = session_factory(settings)
    with transaction(factory) as session:
        competitor = repo.upsert_competitor(session, config)
        competitor_id = competitor.id
    return CompetitorContext(settings, config, adapter, factory, competitor_id)
