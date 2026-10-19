"""Operation B: price-only refresh of already known products.

Visits the stored product detail URLs (no category crawl), extracts the current price
tiers with the adapter's price parser and appends observations through the same
persistence function as the catalog sync. Product descriptions, characteristics and
images are left untouched unless ``update_details`` is explicitly requested.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.core.adapter import CompetitorAdapter
from competitor_catalog.core.errors import PageError
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import Product
from competitor_catalog.db.session import transaction
from competitor_catalog.http.base import FatalFetchError, Fetcher, FetchError, TargetGoneError
from competitor_catalog.services.runs import RunObserver, RunStopped, RunTracker, utcnow

log = logging.getLogger(__name__)


class SelectionError(ValueError):
    pass


@dataclass
class PriceRefreshOptions:
    category: str | None = None  # category id, external id or URL: the whole subtree is refreshed
    references: list[str] = field(default_factory=list)
    retry_failed_run_id: int | None = None  # only products that failed in that run
    skip_fresh_hours: float | None = None  # skip products priced less than N hours ago
    include_missing: bool = False
    update_details: bool = False
    limit: int | None = None


@dataclass
class PriceRefreshResult:
    run_id: int
    status: str
    counters: dict[str, int]
    product_ids: list[int]


class PriceRefresh:
    def __init__(
        self,
        adapter: CompetitorAdapter,
        fetcher: Fetcher,
        factory: sessionmaker[Session],
        competitor_id: int,
        observer: RunObserver | None = None,
    ):
        self.adapter = adapter
        self.fetcher = fetcher
        self.factory = factory
        self.competitor_id = competitor_id
        self.observer = observer or RunObserver()

    def select_products(self, options: PriceRefreshOptions) -> list[Product]:
        with transaction(self.factory) as session:
            category_ids = None
            if options.category:
                category = repo.find_category_by_any(session, self.competitor_id, options.category)
                if category is None:
                    raise SelectionError(f"unknown category {options.category!r}")
                category_ids = repo.category_subtree_ids(session, self.competitor_id, category.id)
            product_ids = None
            if options.retry_failed_run_id is not None:
                product_ids = repo.failed_product_ids(session, self.competitor_id, options.retry_failed_run_id)
            fresher_than = (
                utcnow() - timedelta(hours=options.skip_fresh_hours) if options.skip_fresh_hours is not None else None
            )
            stmt = repo.select_products_for_price_refresh(
                self.competitor_id,
                category_ids=category_ids,
                references=options.references or None,
                product_ids=product_ids,
                include_missing=options.include_missing,
                fresher_than=fresher_than,
                limit=options.limit,
            )
            return list(session.scalars(stmt))

    def run(self, options: PriceRefreshOptions | None = None) -> PriceRefreshResult:
        options = options or PriceRefreshOptions()
        products = self.select_products(options)
        tracker = RunTracker(self.factory, self.competitor_id, "price_refresh", self.observer)
        params: dict[str, Any] = {
            "category": options.category,
            "references": options.references,
            "retry_failed_run_id": options.retry_failed_run_id,
            "skip_fresh_hours": options.skip_fresh_hours,
            "include_missing": options.include_missing,
            "update_details": options.update_details,
            "limit": options.limit,
            "selected_products": len(products),
        }
        run_id = tracker.start(params, full_scope=False)
        status, error = "failed", None
        failures = 0
        try:
            self.observer.progress("refresh_prices", 0, len(products))
            for index, product in enumerate(products, start=1):
                self.observer.check_stop()
                try:
                    ok = self._refresh_one(tracker, product, options)
                except FatalFetchError:
                    raise
                except Exception as exc:  # isolate unexpected errors (DB constraint, parser bug...)
                    log.exception("unexpected error while refreshing %s", product.url)
                    tracker.bump(products_failed=1)
                    tracker.add_item_tx(
                        item_type="product", url=product.url, status="failed", product_id=product.id, error=exc
                    )
                    ok = False
                if not ok:
                    failures += 1
                if index % 10 == 0:
                    log.info("price refresh %d/%d", index, len(products))
                tracker.flush(_credits(self.fetcher))
                self.observer.progress("refresh_prices", index, len(products))
            status = "succeeded" if failures == 0 else "partial"
        except FatalFetchError as exc:
            error = f"fatal fetch error: {exc}"
            log.error(error)
        except KeyboardInterrupt:
            status, error = "aborted", "interrupted by user"
            raise
        except RunStopped as exc:
            status, error = "aborted", f"stopped: {exc}"
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("price refresh run #%s crashed", run_id)
            raise
        finally:
            tracker.finish(status, error=error, credits_used=_credits(self.fetcher))
        return PriceRefreshResult(run_id, status, tracker.snapshot(), [p.id for p in products])

    def _refresh_one(self, tracker: RunTracker, product: Product, options: PriceRefreshOptions) -> bool:
        def failed(status: str, exc: BaseException, http_status: int | None = None) -> bool:
            tracker.bump(products_failed=1)
            tracker.add_item_tx(
                item_type="product",
                url=product.url,
                status=status,
                product_id=product.id,
                error=exc,
                http_status=http_status,
            )
            return False

        try:
            page = self.fetcher.fetch(product.url)
            tracker.bump(pages_fetched=1)
        except FatalFetchError:
            raise
        except TargetGoneError as exc:
            tracker.bump(pages_failed=1)
            return failed("not_found", exc, exc.status_code)
        except FetchError as exc:
            tracker.bump(pages_failed=1)
            return failed("failed", exc, exc.status_code)

        try:
            price_page = self.adapter.parse_prices(page)
            details = self.adapter.parse_product(page) if options.update_details else None
        except PageError as exc:
            tracker.bump(pages_failed=1)
            return failed("failed", exc)

        identity_changed = (
            price_page.catalog_reference != product.catalog_reference
            if product.catalog_reference
            else price_page.url != product.url  # URL-identity products: the page must be the same
        )
        if identity_changed:
            # Redirected to another product (or a listing): never attach its prices to ours.
            return failed(
                "failed",
                PageError(
                    f"identity mismatch: expected {product.catalog_reference}, page shows "
                    f"{price_page.catalog_reference} ({price_page.url})"
                ),
            )

        snapshot = price_page.prices
        warnings = list(snapshot.warnings)
        with transaction(self.factory) as session:
            db_product = session.get(Product, product.id)
            if details is not None:
                category = (
                    repo.find_category(session, self.competitor_id, details.category) if details.category else None
                )
                repo.upsert_product(
                    session,
                    self.competitor_id,
                    details,
                    category_id=category.id if category else None,
                    run_id=tracker.run_id,
                    now=utcnow(),
                    mark_seen=False,  # catalog presence is the catalog sync's business
                )
            count = repo.record_price_snapshot(
                session, db_product, snapshot, run_id=tracker.run_id, observed_at=page.fetched_at
            )
            if not snapshot.price_found:
                warnings.append(f"no price: {snapshot.reason}")
            tracker.add_item(
                session,
                item_type="product",
                url=product.url,
                status="succeeded" if snapshot.price_found else "no_price",
                product_id=product.id,
                warnings=warnings,
            )
        tracker.bump(
            products_seen=1,
            observations_created=count,
            products_without_price=int(not snapshot.price_found),
            products_updated=int(details is not None),
        )
        return True


def _credits(fetcher: Fetcher) -> int | None:
    stats = getattr(fetcher, "stats", None)
    return getattr(stats, "credits_used", None)
