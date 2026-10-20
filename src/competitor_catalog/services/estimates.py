"""Duration/cost estimates of a worker task, before it starts and while it runs.

Product matching makes no page request: its estimate counts the products to score and the
texts to embed (Voyage AI requests), see :func:`estimate_matching`.

The unit of work is one HTTP request (one page). The number of requests comes from the
run history of the competitor (last full catalog crawl, products in database), falling
back to the ``estimate_hints`` of the competitor config; the time per request comes from
the measured duration of previous runs, falling back to the configured crawl delay.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from competitor_catalog.core.config import CompetitorConfig
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import (
    MATCH_ENGINE_STATUSES,
    MATCH_HUMAN_STATUSES,
    Category,
    Product,
    ProductEmbedding,
    ProductMatch,
    ScrapeRun,
    ScrapeRunItem,
    WorkerJob,
)
from competitor_catalog.matching.config import EmbeddingSettings, MatchingConfig
from competitor_catalog.services.runs import utcnow

# Measured duration of one Scrape.do request (no rendering). The crawl delay counts from
# request start, so a request takes the longer of the two.
DEFAULT_REQUEST_SECONDS = 1.5
MIN_PAGES_FOR_MEASURE = 20
# Product matching: one Voyage request per batch of texts (paced by the account's rate
# limits), scoring is CPU-bound.
EMBEDDING_REQUEST_SECONDS = 3.0
EMBEDDING_TOKENS_PER_TEXT = 100  # measured: title + description of a packaging product
MATCH_SECONDS_PER_PRODUCT = 0.02


@dataclass
class Estimate:
    task: str
    listing_pages: int | None = None  # catalog sync only
    products: int | None = None  # product pages to fetch (matching: products to score)
    embeddings: int | None = None  # matching only: product texts to embed (not cached)
    requests: int | None = None
    seconds_per_request: float = 0.0
    seconds: float | None = None
    credits: int | None = None
    basis: list[str] = field(default_factory=list)  # human-readable provenance of each figure

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def seconds_per_request(session: Session, competitor_id: int, config: CompetitorConfig) -> tuple[float, str]:
    """Average wall-clock time per request over recent finished runs of this competitor."""
    rows = session.execute(
        select(
            ScrapeRun.id, ScrapeRun.started_at, ScrapeRun.finished_at, ScrapeRun.pages_fetched, ScrapeRun.pages_failed
        )
        .where(
            ScrapeRun.competitor_id == competitor_id,
            ScrapeRun.finished_at.is_not(None),
            ScrapeRun.pages_fetched + ScrapeRun.pages_failed >= MIN_PAGES_FOR_MEASURE,
        )
        .order_by(ScrapeRun.finished_at.desc())
        .limit(5)
    ).all()
    # time spent paused (worker jobs) is not crawl time
    paused = {
        run_id: float((progress or {}).get("paused_seconds") or 0)
        for run_id, progress in session.execute(
            select(WorkerJob.scrape_run_id, WorkerJob.progress).where(WorkerJob.scrape_run_id.in_([r.id for r in rows]))
        )
    }
    seconds = sum((r.finished_at - r.started_at).total_seconds() - paused.get(r.id, 0.0) for r in rows)
    pages = sum(r.pages_fetched + r.pages_failed for r in rows)
    if pages:
        return seconds / pages, f"{seconds / pages:.1f}s/request measured over {len(rows)} previous run(s)"
    value = max(config.request_interval_seconds, DEFAULT_REQUEST_SECONDS)
    return value, (
        f"{value:.1f}s/request = longer of crawl delay {config.request_interval_seconds:g}s "
        f"and typical request time {DEFAULT_REQUEST_SECONDS:g}s"
    )


def credits_per_request(session: Session, competitor_id: int) -> float:
    credits, pages = session.execute(
        select(func.sum(ScrapeRun.credits_used), func.sum(ScrapeRun.pages_fetched)).where(
            ScrapeRun.competitor_id == competitor_id, ScrapeRun.credits_used > 0
        )
    ).one()
    return float(credits) / float(pages) if credits and pages else 1.0


def listing_pages_estimate(session: Session, competitor_id: int, config: CompetitorConfig) -> tuple[int | None, str]:
    def listing_items(run_id: int) -> int:
        return session.scalar(
            select(func.count())
            .select_from(ScrapeRunItem)
            .where(ScrapeRunItem.scrape_run_id == run_id, ScrapeRunItem.item_type == "listing_page")
        )

    done = session.scalar(
        select(ScrapeRun.id)
        .where(
            ScrapeRun.competitor_id == competitor_id,
            ScrapeRun.operation == "catalog_discovery",
            ScrapeRun.full_scope.is_(True),
            ScrapeRun.status.in_(("succeeded", "partial")),
        )
        .order_by(ScrapeRun.finished_at.desc())
        .limit(1)
    )
    if done is not None:
        count = listing_items(done)
        if count:
            return count, f"{count} listing pages in the last full catalog sync (run #{done})"
    hint = config.estimate_hints.get("listing_pages")
    if hint:
        return int(hint), f"{hint} listing pages (estimate_hints of the competitor config)"
    categories = session.scalar(
        select(func.count()).select_from(Category).where(Category.competitor_id == competitor_id)
    )
    if categories:
        return categories, f"{categories} known categories (at least one listing page each)"
    return None, "listing pages unknown (no history, no estimate_hints)"


def estimate_job(
    session: Session,
    competitor_id: int,
    config: CompetitorConfig,
    task: str,
    options: dict[str, Any],
    *,
    matching_config: MatchingConfig | None = None,
) -> Estimate:
    if task == "matching":
        return estimate_matching(session, competitor_id, config, options, matching_config)
    spr, spr_basis = seconds_per_request(session, competitor_id, config)
    estimate = Estimate(task=task, seconds_per_request=round(spr, 2), basis=[spr_basis])
    if task == "catalog_sync":
        partial = any(options.get(k) for k in ("root_urls", "max_listing_pages", "max_products", "skip_products"))
        if partial:
            estimate.basis.append("partial scope: figures assume the full catalog, the run will be shorter")
        if options.get("retry_failed_run_id") is not None:
            failed = repo.count_failed_items(session, int(options["retry_failed_run_id"]))
            estimate.listing_pages = failed.get("listing_page", 0)
            estimate.products = failed.get("product", 0)
            estimate.requests = estimate.listing_pages + estimate.products
            estimate.basis.append(
                f"retry of run #{options['retry_failed_run_id']}: {estimate.listing_pages} failed listing page(s) "
                f"(plus their sub-pages, unknown) and {estimate.products} failed product page(s)"
            )
            estimate.seconds = round(estimate.requests * spr)
            estimate.credits = round(estimate.requests * credits_per_request(session, competitor_id))
            return estimate
        pages, basis = listing_pages_estimate(session, competitor_id, config)
        if options.get("max_listing_pages") and pages:
            pages = min(pages, int(options["max_listing_pages"]))
        if options.get("resume_within_hours") and pages:
            since = utcnow() - timedelta(hours=float(options["resume_within_hours"]))
            replayed = len(repo.cached_listing_urls(session, competitor_id, since))
            if replayed:
                pages = max(0, pages - replayed)
                basis += f"; {replayed} cached by an unfinished sync (replayed, no request)"
        estimate.listing_pages = pages
        estimate.basis.append(basis)
        known = session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.competitor_id == competitor_id, Product.status == "active")
        )
        hint = int(config.estimate_hints.get("products") or 0)
        products: int | None
        if options.get("skip_products"):
            products, basis = 0, "products not synced (skip_products)"
        elif competitor_has_full_catalog(session, competitor_id) is False and hint > known:
            # the database only holds a sample: the catalog size comes from the hint
            products = hint
            basis = f"{hint} products (estimate_hints; only {known} in database, no full catalog sync yet)"
        elif known:
            recent = 0
            if options.get("resume_within_hours"):
                cutoff = utcnow() - timedelta(hours=float(options["resume_within_hours"]))
                recent = session.scalar(
                    select(func.count())
                    .select_from(Product)
                    .where(
                        Product.competitor_id == competitor_id,
                        Product.status == "active",
                        Product.details_updated_at >= cutoff,
                    )
                )
            products = known - recent
            basis = f"{known} active products in database" + (f", {recent} synced recently (skipped)" if recent else "")
        elif hint:
            products = hint
            basis = f"{products} products (estimate_hints of the competitor config)"
        else:
            products, basis = None, "number of products unknown (no history, no estimate_hints)"
        if options.get("max_products") is not None and products is not None:
            products = min(products, int(options["max_products"]))
        estimate.products = products
        estimate.basis.append(basis)
        if pages is not None and products is not None:
            estimate.requests = pages + products
    elif task == "price_refresh":
        estimate.products = count_price_refresh_selection(session, competitor_id, options)
        estimate.requests = estimate.products
        estimate.basis.append(f"{estimate.products} products selected by the options")
    else:
        raise ValueError(f"unknown task {task!r}")
    if estimate.requests is not None:
        estimate.seconds = round(estimate.requests * spr)
        estimate.credits = round(estimate.requests * credits_per_request(session, competitor_id))
    return estimate


def estimate_matching(
    session: Session,
    competitor_id: int,
    config: CompetitorConfig,
    options: dict[str, Any],
    matching_config: MatchingConfig | None,
) -> Estimate:
    from competitor_catalog.matching.service import eligible_products, reference_competitor

    mode = options.get("mode", "incremental")
    eligible = eligible_products(competitor_id)
    rows = select(ProductMatch.product_id).where(ProductMatch.competitor_id == competitor_id)

    def count(stmt) -> int:
        return session.scalar(select(func.count()).select_from(stmt.subquery()))

    deprecated = count(rows.where(ProductMatch.status == "deprecated"))
    never = count(eligible.where(Product.id.not_in(rows)))
    basis = [f"{deprecated} deprecated match(es)"]
    if mode == "deprecated":
        products = deprecated
    elif mode == "all":
        decided = count(rows.where(ProductMatch.status.in_(MATCH_HUMAN_STATUSES)))
        products = count(eligible) - decided
        basis.append(f"every eligible product except {decided} human decision(s)")
    else:
        products = never + deprecated
        basis.append(f"{never} product(s) never matched")
        if matching_config is not None:
            outdated = count(
                rows.where(
                    ProductMatch.status.in_(MATCH_ENGINE_STATUSES),
                    ProductMatch.config_hash.is_distinct_from(matching_config.config_hash(config.matching)),
                )
            )
            if outdated:
                products += outdated
                basis.append(f"{outdated} engine result(s) computed with another configuration")
        basis.append("plus the engine results to refresh if the reference catalog changed (known at run time)")
    model_filter = [ProductEmbedding.product_id == Product.id]
    if matching_config is not None:
        model_filter += [
            ProductEmbedding.model == matching_config.embedding.model,
            ProductEmbedding.dimensions == matching_config.embedding.dimensions,
        ]
    reembed = bool(options.get("reembed"))
    missing = ~select(ProductEmbedding.product_id).where(*model_filter).exists()
    embeddings = products if reembed else min(products, count(eligible.where(missing)))
    reference = reference_competitor(session, matching_config) if matching_config else None
    if reference is not None:
        reference_rows = eligible_products(reference.id)
        reference_missing = count(reference_rows if reembed else reference_rows.where(missing))
        embeddings += reference_missing
        basis.append(
            f"reembed: {reference_missing} reference product(s) embedded again"
            if reembed
            else f"{reference_missing} reference product(s) without embedding"
        )
    settings = matching_config.embedding if matching_config else EmbeddingSettings()
    tokens = embeddings * EMBEDDING_TOKENS_PER_TEXT
    per_request = max(1, min(settings.batch_size, settings.batch_token_budget // EMBEDDING_TOKENS_PER_TEXT))
    requests = -(-embeddings // per_request) if embeddings else 0
    embedding_seconds = max(
        requests * EMBEDDING_REQUEST_SECONDS,
        (requests - 1) * 60 / settings.requests_per_minute if requests else 0,
        (tokens - settings.tokens_per_minute) * 60 / settings.tokens_per_minute
        if tokens > settings.tokens_per_minute
        else 0,
    )
    basis.append(
        f"{embeddings} text(s) to embed (~{tokens} tokens) in {requests} Voyage request(s) at "
        f"{settings.requests_per_minute} requests / {settings.tokens_per_minute} tokens per minute"
        + ("" if reembed else "; texts already embedded (by any product) are reused")
    )
    return Estimate(
        task="matching",
        products=products,
        embeddings=embeddings,
        requests=requests,
        seconds_per_request=round(embedding_seconds / requests, 1) if requests else EMBEDDING_REQUEST_SECONDS,
        seconds=round(embedding_seconds + products * MATCH_SECONDS_PER_PRODUCT + 5),
        credits=0,
        basis=basis,
    )


def competitor_has_full_catalog(session: Session, competitor_id: int) -> bool:
    return (
        session.scalar(
            select(ScrapeRun.id)
            .where(
                ScrapeRun.competitor_id == competitor_id,
                ScrapeRun.operation == "catalog_discovery",
                ScrapeRun.full_scope.is_(True),
                ScrapeRun.status.in_(("succeeded", "partial")),
            )
            .limit(1)
        )
        is not None
    )


def count_price_refresh_selection(session: Session, competitor_id: int, options: dict[str, Any]) -> int:
    category_ids = None
    if options.get("category"):
        category = repo.find_category_by_any(session, competitor_id, str(options["category"]))
        category_ids = repo.category_subtree_ids(session, competitor_id, category.id) if category else []
    product_ids = None
    if options.get("retry_failed_run_id") is not None:
        product_ids = repo.failed_product_ids(session, competitor_id, int(options["retry_failed_run_id"]))
    fresh = options.get("skip_fresh_hours")
    stmt = repo.select_products_for_price_refresh(
        competitor_id,
        category_ids=category_ids,
        references=options.get("references") or None,
        product_ids=product_ids,
        include_missing=bool(options.get("include_missing")),
        fresher_than=utcnow() - timedelta(hours=float(fresh)) if fresh is not None else None,
        limit=options.get("limit"),
    )
    return session.scalar(select(func.count()).select_from(stmt.subquery()))
