"""Operation A: catalog discovery and product synchronization (generic, adapter-driven).

1. Breadth-first crawl of listing pages from the configured roots: every page is
   classified by the adapter, its category is upserted (hierarchy from the page's own
   navigation/breadcrumb), its children, product links and next page are queued.
2. Every discovered product page is fetched and parsed; the product is upserted
   (idempotent: existing products are updated, new ones created) and its current prices
   are appended through the shared price persistence used by the price refresh.
3. Only a *complete* run over the *full configured scope* may flag categories/products it
   did not see as ``missing``; a partial/failed run never does.

Every listing page fetched is also kept (compressed) in ``listing_page_cache`` until a full
sync completes. A sync started with ``resume_within_hours`` (the default of the API and of
restarted/auto-resumed jobs) replays the cached pages fetched within that window instead of
requesting them again: resuming an interrupted sync costs no listing request, and its
crawl, completeness checks and missing-flagging work exactly as for a fresh crawl.

``retry_failed_run_id`` replaces the full crawl by the pages that failed in a previous run:
its failed product pages, and its failed listing pages (crawled again with their subtree,
whose products were never discovered). Such a run is never full scope.
"""

from __future__ import annotations

import logging
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from competitor_catalog.core.adapter import CompetitorAdapter
from competitor_catalog.core.errors import PageError, PageParseError
from competitor_catalog.core.types import PageKind, ProductLink
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import Product
from competitor_catalog.db.session import transaction
from competitor_catalog.http.base import FatalFetchError, FetchedPage, Fetcher, FetchError, TargetGoneError
from competitor_catalog.services.runs import RunObserver, RunStopped, RunTracker, utcnow

log = logging.getLogger(__name__)

LISTING_CACHE_MAX_AGE = timedelta(days=7)  # older cached pages are purged at the next sync


@dataclass
class CatalogSyncOptions:
    root_urls: list[str] | None = None  # None = configured roots (full scope)
    max_listing_pages: int | None = None
    max_products: int | None = None
    skip_products: bool = False
    # Safety net: refuse to flag more than this share of active products as missing.
    max_missing_ratio: float = 0.2
    # Resume support: products whose details were synced less than N hours ago (typically by
    # an interrupted previous run) are not fetched again, only confirmed as seen.
    resume_within_hours: float | None = None
    allow_mass_missing: bool = False
    # Only the pages that failed in that run of the same competitor (no full crawl).
    retry_failed_run_id: int | None = None

    @property
    def is_full_scope(self) -> bool:
        return (
            self.root_urls is None
            and self.max_listing_pages is None
            and self.max_products is None
            and not self.skip_products
            and self.retry_failed_run_id is None
        )


@dataclass
class CatalogSyncResult:
    run_id: int
    status: str
    counters: dict[str, int]
    category_urls: list[str] = field(default_factory=list)
    product_urls: list[str] = field(default_factory=list)
    incomplete_reasons: list[str] = field(default_factory=list)


class CatalogSync:
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
        self._replayable: set[str] = set()

    def run(self, options: CatalogSyncOptions | None = None) -> CatalogSyncResult:
        options = options or CatalogSyncOptions()
        retry_products: list[str] = []
        if options.retry_failed_run_id is not None:
            with transaction(self.factory) as session:
                roots, retry_products = repo.failed_catalog_urls(
                    session, self.competitor_id, options.retry_failed_run_id
                )
        else:
            roots = [self.adapter.normalize_url(u) for u in (options.root_urls or self.adapter.config.root_urls)]
        tracker = RunTracker(self.factory, self.competitor_id, "catalog_discovery", self.observer)
        params: dict[str, Any] = {
            "root_urls": roots,
            "max_listing_pages": options.max_listing_pages,
            "max_products": options.max_products,
            "skip_products": options.skip_products,
            "retry_failed_run_id": options.retry_failed_run_id,
        }
        run_id = tracker.start(params, full_scope=options.is_full_scope)
        self._prepare_replay(options)
        # Retried listing pages are not crawl roots: one failing again is a failed page, not a fatal error.
        state = _CrawlState(roots=roots, critical_roots=options.retry_failed_run_id is None)
        for url in retry_products:
            state.add_product(ProductLink(url=url))
        status, error = "failed", None
        try:
            self._crawl_listings(tracker, state, options)
            tracker.stats["product_links_discovered"] = len(state.product_links)
            tracker.stats["duplicate_product_links_ignored"] = state.duplicate_product_links
            if not options.skip_products:
                self._sync_products(tracker, state, options)
            self.observer.check_stop()
            self.observer.progress("finalize", 0, 1)
            self._record_unprocessed_links(tracker, state)
            with transaction(self.factory) as session:
                linked = repo.resolve_category_parents(session, self.competitor_id)
            tracker.stats["late_parent_links"] = linked
            complete = not state.incomplete_reasons
            if complete and options.is_full_scope:
                self._mark_missing(tracker, run_id, options, state)
            status = "succeeded" if complete else "partial"
            if options.is_full_scope:  # the whole tree was crawled: nothing left to resume
                with transaction(self.factory) as session:
                    repo.purge_listing_cache(session, self.competitor_id)
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
            log.exception("catalog run #%s crashed", run_id)
            raise
        finally:
            tracker.stats["incomplete_reasons"] = state.incomplete_reasons[:200]
            tracker.stats["completeness_mismatches"] = state.mismatches[:200]
            tracker.finish(status, error=error, credits_used=_credits(self.fetcher))
        return CatalogSyncResult(
            run_id=run_id,
            status=status,
            counters=tracker.snapshot(),
            category_urls=list(state.category_urls),
            product_urls=[link.url for link in state.product_links.values()],
            incomplete_reasons=state.incomplete_reasons,
        )

    # ------------------------------------------------------------------ listings
    def _crawl_listings(self, tracker: RunTracker, state: _CrawlState, options: CatalogSyncOptions) -> None:
        queue: deque[str] = deque(state.roots)
        queued: set[str] = set(state.roots)
        pages = 0
        self.observer.progress("crawl_listings", 0, None, pending=len(queue))
        while queue:
            self.observer.check_stop()
            if options.max_listing_pages is not None and pages >= options.max_listing_pages:
                state.incomplete(f"listing page limit {options.max_listing_pages} reached")
                break
            url = queue.popleft()
            pages += 1
            is_root = url in state.roots
            critical = is_root and state.critical_roots
            try:
                self._process_listing(tracker, state, url, is_root, queue, queued, critical=critical)
            except FatalFetchError:
                raise
            except Exception as exc:  # one broken page must not kill a multi-hour crawl
                log.exception("unexpected error on listing page %s", url)
                self._page_failed(tracker, state, url, exc, critical=critical)
            tracker.flush(_credits(self.fetcher))
            self.observer.progress("crawl_listings", pages, None, pending=len(queue))
        self.observer.progress("crawl_listings", pages, pages)

        for owner, (expected, found) in state.expected_counts().items():
            if expected is not None and found < expected:
                state.mismatches.append({"url": owner, "expected": expected, "found": found})
                state.incomplete(f"{owner}: site announces {expected} items, {found} collected")
            elif expected is not None and found > expected:
                state.mismatches.append({"url": owner, "expected": expected, "found": found})

    def _process_listing(
        self,
        tracker: RunTracker,
        state: _CrawlState,
        url: str,
        is_root: bool,
        queue: deque[str],
        queued: set[str],
        *,
        critical: bool,
    ) -> None:
        page = self._fetch(tracker, url, "listing_page", state, critical=critical)
        if page is None:
            return
        try:
            kind = self.adapter.classify(page)
            if kind is PageKind.PRODUCT:
                # A navigation link that leads straight to a product page.
                state.add_product(ProductLink(url=self.adapter.normalize_url(page.final_url)), page)
                tracker.add_item_tx(item_type="listing_page", url=url, status="skipped", warnings=["is a product page"])
                return
            listing = self.adapter.parse_listing(page)
        except PageError as exc:
            self._page_failed(tracker, state, url, exc, critical=critical)
            return

        category = listing.category
        continuation_of = state.page_owner.get(url)
        if continuation_of is not None:
            # Pagination page: same category as the first page (whatever its canonical URL).
            tracker.add_item_tx(
                item_type="listing_page",
                url=url,
                status="succeeded",
                category_id=state.category_ids.get(continuation_of),
                warnings=listing.warnings,
            )
        elif category is not None:
            if is_root:
                state.root_canonicals.add(category.url)
            ancestors = [a.url for a in category.ancestors if a.url]
            if not ancestors and not is_root and category.url not in state.root_canonicals:
                # Without navigation path the scope cannot be decided: never assume "outside".
                raise PageParseError("category page without navigation ancestors (breadcrumb)", url=url)
            if not self.adapter.is_in_scope(category.url, ancestors, state.root_canonicals):
                tracker.add_item_tx(
                    item_type="listing_page", url=url, status="skipped", warnings=["outside the crawl scope"]
                )
                return
            with transaction(self.factory) as session:
                cat, _ = repo.upsert_category(
                    session, self.competitor_id, category, run_id=tracker.run_id, now=utcnow()
                )
                tracker.add_item(
                    session,
                    item_type="listing_page",
                    url=url,
                    status="succeeded",
                    category_id=cat.id,
                    warnings=listing.warnings,
                )
                category_id = cat.id
            if category.url not in state.category_urls:
                state.category_urls.append(category.url)
                tracker.bump(categories_seen=1)
            state.category_ids[category.url] = category_id
            state.track_expected(category.url, listing.expected_item_count)
        else:
            tracker.add_item_tx(item_type="listing_page", url=url, status="succeeded", warnings=listing.warnings)

        owner = continuation_of or (category.url if category else url)
        for child in listing.child_category_urls:
            state.count_item(owner, child)
            if child not in queued:
                queued.add(child)
                queue.append(child)
        for link in listing.product_links:
            state.count_item(owner, link.reference or link.url)
            state.add_product(link)
        if listing.next_page_url and listing.next_page_url not in queued:
            queued.add(listing.next_page_url)
            state.page_owner[listing.next_page_url] = owner
            # Continue the pagination right away so that a listing is read in one go.
            queue.appendleft(listing.next_page_url)

    # ------------------------------------------------------------------ products
    def _sync_products(self, tracker: RunTracker, state: _CrawlState, options: CatalogSyncOptions) -> None:
        # Pages already fetched during the listing crawl first, so that the same SKU reached
        # through a family page afterwards is recognised and not fetched twice.
        links = sorted(state.product_links.items(), key=lambda kv: kv[0] not in state.prefetched)
        processed_refs: set[str] = set()
        recent = self._recently_synced(options)
        to_fetch = sum(
            1
            for key, link in links
            if key in state.prefetched or not (recent.get(f"ref:{link.reference}") or recent.get(f"url:{link.url}"))
        )
        if options.max_products is not None:
            to_fetch = min(to_fetch, options.max_products)
        processed = 0
        self.observer.progress("sync_products", 0, to_fetch)
        for key, link in links:
            self.observer.check_stop()
            if link.reference and link.reference in processed_refs:
                continue
            recent_id = recent.get(f"ref:{link.reference}") or recent.get(f"url:{link.url}")
            if recent_id is not None and key not in state.prefetched:
                self._confirm_recent(tracker, state, key, link, recent_id)
                continue
            if options.max_products is not None and processed >= options.max_products:
                state.incomplete(f"product limit {options.max_products} reached")
                break
            processed += 1
            state.processed_product_keys.add(key)
            try:
                reference = self._process_product(tracker, state, key, link)
                if reference:
                    processed_refs.add(reference)
            except FatalFetchError:
                raise
            except Exception as exc:  # isolate unexpected errors (DB constraint, parser bug...)
                log.exception("unexpected error on product %s", link.url)
                self._page_failed(tracker, state, link.url, exc, item_type="product")
                tracker.bump(products_failed=1)
            if processed % 10 == 0:
                log.info("products %d/%d", processed, len(links))
            tracker.flush(_credits(self.fetcher))
            self.observer.progress("sync_products", processed, max(to_fetch, processed))

    def _process_product(self, tracker: RunTracker, state: _CrawlState, key: str, link: ProductLink) -> str | None:
        page = state.prefetched.pop(key, None) or self._fetch(tracker, link.url, "product", state)
        if page is None:
            tracker.bump(products_failed=1)
            return None
        try:
            data = self.adapter.parse_product(page)
        except PageError as exc:
            self._page_failed(tracker, state, link.url, exc, item_type="product")
            tracker.bump(products_failed=1)
            return None
        warnings = list(data.warnings)
        if link.reference and data.catalog_reference and link.reference != data.catalog_reference:
            warnings.append(f"listing announced reference {link.reference}, page shows {data.catalog_reference}")
        with transaction(self.factory) as session:
            category = repo.find_category(session, self.competitor_id, data.category) if data.category else None
            if data.category and category is None:
                warnings.append(f"category {data.category.url} not found in database")
            product, created = repo.upsert_product(
                session,
                self.competitor_id,
                data,
                category_id=category.id if category else None,
                run_id=tracker.run_id,
                now=utcnow(),
            )
            observations = repo.record_price_snapshot(
                session, product, data.prices, run_id=tracker.run_id, observed_at=page.fetched_at
            )
            if not data.prices.price_found:
                warnings.append(f"no price: {data.prices.reason}")
            tracker.add_item(
                session,
                item_type="product",
                url=link.url,
                status="succeeded" if data.prices.price_found else "no_price",
                product_id=product.id,
                warnings=warnings,
            )
        tracker.bump(
            products_seen=1,
            products_created=int(created),
            products_updated=int(not created),
            observations_created=observations,
            products_without_price=int(not data.prices.price_found),
        )
        return data.catalog_reference

    def _record_unprocessed_links(self, tracker: RunTracker, state: _CrawlState) -> None:
        """Persist discovered product URLs that were not synced (--skip-products / limits)."""
        pending = [link for key, link in state.product_links.items() if key not in state.processed_product_keys]
        if not pending:
            return
        with transaction(self.factory) as session:
            for link in pending:
                tracker.add_item(
                    session,
                    item_type="product",
                    url=link.url,
                    status="skipped",
                    warnings=[f"discovered, not synced in this run (reference {link.reference})"],
                )

    def _recently_synced(self, options: CatalogSyncOptions) -> dict[str, int]:
        if not options.resume_within_hours or options.retry_failed_run_id is not None:
            return {}  # a retry refetches the failed pages whatever their last sync
        cutoff = utcnow() - timedelta(hours=options.resume_within_hours)
        with transaction(self.factory) as session:
            rows = session.execute(
                select(Product.id, Product.catalog_reference, Product.url).where(
                    Product.competitor_id == self.competitor_id,
                    Product.status == "active",
                    Product.details_updated_at >= cutoff,
                )
            ).all()
        recent: dict[str, int] = {}
        for product_id, reference, url in rows:
            if reference:
                recent[f"ref:{reference}"] = product_id
            recent[f"url:{url}"] = product_id
        return recent

    def _confirm_recent(
        self, tracker: RunTracker, state: _CrawlState, key: str, link: ProductLink, product_id: int
    ) -> None:
        """Listed again and synced recently: count it as seen without fetching it again."""
        state.processed_product_keys.add(key)
        with transaction(self.factory) as session:
            session.execute(
                update(Product)
                .where(Product.id == product_id)
                .values(last_seen_run_id=tracker.run_id, last_seen_at=utcnow(), status="active", missing_since=None)
            )
            tracker.add_item(
                session,
                item_type="product",
                url=link.url,
                status="skipped",
                product_id=product_id,
                warnings=["details synced recently (resume): not fetched again"],
            )
        tracker.bump(products_seen=1)
        tracker.stats["products_resumed"] = tracker.stats.get("products_resumed", 0) + 1

    # ------------------------------------------------------------------ listing page cache
    def _prepare_replay(self, options: CatalogSyncOptions) -> None:
        with transaction(self.factory) as session:
            repo.purge_listing_cache(session, self.competitor_id, older_than=utcnow() - LISTING_CACHE_MAX_AGE)
            if options.resume_within_hours and options.retry_failed_run_id is None:
                since = utcnow() - timedelta(hours=options.resume_within_hours)
                self._replayable = repo.cached_listing_urls(session, self.competitor_id, since)
        if self._replayable:
            log.info("resume: %d listing page(s) replayed from the cache", len(self._replayable))

    def _replay(self, tracker: RunTracker, url: str) -> FetchedPage | None:
        self._replayable.discard(url)
        with transaction(self.factory) as session:
            cached = repo.load_listing_page(session, self.competitor_id, url)
            if cached is None:
                return None
            page = FetchedPage(
                url,
                cached.final_url,
                cached.status_code,
                zlib.decompress(cached.body).decode("utf-8"),
                cached.fetched_at,
                {"replayed_from_cache": True},
            )
        tracker.stats["listing_pages_replayed"] = tracker.stats.get("listing_pages_replayed", 0) + 1
        return page

    def _cache(self, tracker: RunTracker, page: FetchedPage) -> None:
        try:
            with transaction(self.factory) as session:
                repo.store_listing_page(session, self.competitor_id, tracker.run_id, page)
        except Exception:  # the cache only saves requests on a resume: never fail the crawl for it
            log.exception("could not cache listing page %s", page.requested_url)

    # ------------------------------------------------------------------ helpers
    def _fetch(
        self, tracker: RunTracker, url: str, item_type: str, state: _CrawlState, *, critical: bool = False
    ) -> FetchedPage | None:
        if item_type == "listing_page" and url in self._replayable:
            page = self._replay(tracker, url)
            if page is not None:
                return page
        try:
            page = self.fetcher.fetch(url)
            tracker.bump(pages_fetched=1)
            if item_type == "listing_page":
                self._cache(tracker, page)
            return page
        except FatalFetchError:
            raise
        except TargetGoneError as exc:
            tracker.bump(pages_failed=1)
            tracker.add_item_tx(
                item_type=item_type, url=url, status="not_found", error=exc, http_status=exc.status_code
            )
            if critical:
                state.incomplete(f"root {url} not found")
                raise FatalFetchError(f"crawl root {url} returned {exc.status_code}", url=url) from exc
            # A link present in the navigation leads nowhere: the catalog view is inconsistent,
            # so nothing can be concluded about products that were not seen.
            state.incomplete(f"{item_type} {url} not found ({exc.status_code})")
            return None
        except FetchError as exc:
            tracker.bump(pages_failed=1)
            tracker.add_item_tx(item_type=item_type, url=url, status="failed", error=exc, http_status=exc.status_code)
            state.incomplete(f"fetch failed for {url}: {exc}")
            if critical:
                raise FatalFetchError(f"crawl root {url} could not be fetched: {exc}", url=url) from exc
            return None

    def _page_failed(
        self,
        tracker: RunTracker,
        state: _CrawlState,
        url: str,
        exc: Exception,
        *,
        critical: bool = False,
        item_type: str = "listing_page",
    ) -> None:
        log.warning("cannot use %s: %s", url, exc)
        tracker.bump(pages_failed=1)
        tracker.add_item_tx(item_type=item_type, url=url, status="failed", error=exc)
        state.incomplete(f"{type(exc).__name__} on {url}: {exc}")
        if critical:
            raise FatalFetchError(f"crawl root {url} is not a usable page: {exc}", url=url) from exc

    def _mark_missing(self, tracker: RunTracker, run_id: int, options: CatalogSyncOptions, state: _CrawlState) -> None:
        with transaction(self.factory) as session:
            unseen, total = repo.count_unseen_active_products(session, self.competitor_id, run_id)
            if total and unseen / total > options.max_missing_ratio and not options.allow_mass_missing:
                msg = (
                    f"{unseen}/{total} active products were not seen: above the {options.max_missing_ratio:.0%} "
                    "safety threshold, nothing marked missing (use --allow-mass-missing after checking)"
                )
                log.warning(msg)
                tracker.stats["missing_marking_skipped"] = msg
                return
            cats, prods = repo.mark_unseen_missing(session, self.competitor_id, run_id, utcnow())
        tracker.bump(categories_marked_missing=cats, products_marked_missing=prods)


class _CrawlState:
    def __init__(self, roots: list[str], *, critical_roots: bool = True):
        self.roots = set(roots)
        self.critical_roots = critical_roots  # a failing root aborts the run
        self.root_canonicals: set[str] = set(roots)
        self.category_urls: list[str] = []
        self.product_links: dict[str, ProductLink] = {}
        self.prefetched: dict[str, FetchedPage] = {}
        self.duplicate_product_links = 0
        self.processed_product_keys: set[str] = set()
        self.page_owner: dict[str, str] = {}  # pagination page URL -> category URL of page 1
        self.category_ids: dict[str, int] = {}
        self.incomplete_reasons: list[str] = []
        self.mismatches: list[dict[str, Any]] = []
        self._expected: dict[str, int | None] = {}
        self._found: dict[str, set[str]] = {}

    def incomplete(self, reason: str) -> None:
        self.incomplete_reasons.append(reason)

    def add_product(self, link: ProductLink, page: FetchedPage | None = None) -> None:
        key = f"ref:{link.reference}" if link.reference else f"url:{link.url}"
        if key in self.product_links:
            self.duplicate_product_links += 1
        self.product_links.setdefault(key, link)
        if page is not None:
            self.prefetched[key] = page

    def track_expected(self, owner: str, expected: int | None) -> None:
        if expected is not None:
            self._expected[owner] = expected

    def count_item(self, owner: str, item: str) -> None:
        self._found.setdefault(owner, set()).add(item)

    def expected_counts(self) -> dict[str, tuple[int | None, int]]:
        return {owner: (exp, len(self._found.get(owner, ()))) for owner, exp in self._expected.items()}


def _credits(fetcher: Fetcher) -> int | None:
    stats = getattr(fetcher, "stats", None)
    return getattr(stats, "credits_used", None)
