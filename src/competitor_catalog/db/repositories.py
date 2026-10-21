"""Persistence logic, independent from any HTML/selectors.

All functions take an open session; transaction boundaries are decided by the services.
"""

from __future__ import annotations

import re
import uuid
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from competitor_catalog.core.config import CompetitorConfig
from competitor_catalog.core.types import CategoryData, CategoryRef, PriceSnapshot, ProductData
from competitor_catalog.core.urls import last_path_segment
from competitor_catalog.db.models import (
    BannedCategory,
    Category,
    Competitor,
    ListingPageCache,
    Product,
    ScrapeObservation,
    ScrapeRun,
    ScrapeRunItem,
)
from competitor_catalog.http.base import FetchedPage

# --------------------------------------------------------------------------- competitors


def upsert_competitor(session: Session, config: CompetitorConfig) -> Competitor:
    competitor = session.scalar(select(Competitor).where(Competitor.key == config.key))
    if competitor is None:
        competitor = Competitor(key=config.key)
        session.add(competitor)
    competitor.name = config.name
    competitor.base_url = config.base_url
    competitor.adapter = config.adapter
    competitor.config = config.as_snapshot()
    competitor.enabled = config.enabled
    session.flush()
    return competitor


def get_competitor(session: Session, key: str) -> Competitor | None:
    return session.scalar(select(Competitor).where(Competitor.key == key))


# --------------------------------------------------------------------------- categories


def find_category(session: Session, competitor_id: int, ref: CategoryRef) -> Category | None:
    """Resolve a category reference: real external id first, canonical URL second."""
    if ref.external_id:
        found = session.scalar(
            select(Category).where(Category.competitor_id == competitor_id, Category.external_id == ref.external_id)
        )
        if found is not None:
            return found
    if ref.url:
        return session.scalar(select(Category).where(Category.competitor_id == competitor_id, Category.url == ref.url))
    return None


def upsert_category(
    session: Session, competitor_id: int, data: CategoryData, *, run_id: int | None, now: datetime
) -> tuple[Category, bool]:
    category = None
    if data.external_id:
        category = session.scalar(
            select(Category).where(Category.competitor_id == competitor_id, Category.external_id == data.external_id)
        )
    by_url = session.scalar(select(Category).where(Category.competitor_id == competitor_id, Category.url == data.url))
    created = False
    if category is None:
        category = by_url
    if category is None:
        category = Category(competitor_id=competitor_id, url=data.url, first_seen_at=now)
        session.add(category)
        created = True
    elif category.url != data.url and by_url is None:
        category.url = data.url  # same real id, new canonical URL (slug change)

    if data.external_id and category.external_id is None:
        category.external_id = data.external_id
    category.name = data.name
    category.slug = last_path_segment(data.url)
    category.kind = data.kind
    category.depth = len(data.ancestors)
    category.breadcrumb = [{"name": a.name, "url": a.url} for a in data.ancestors]
    parent_url = data.parent.url if data.parent else None
    observed = {**data.extra, "title": data.title, "parent_url": parent_url}
    category.details = {**(category.details or {}), **{k: v for k, v in observed.items() if v is not None}}
    category.status = "active"
    category.missing_since = None
    category.last_seen_at = now
    category.last_seen_run_id = run_id
    session.flush()
    if data.parent is not None:
        parent = find_category(session, competitor_id, data.parent)
        if parent is not None and parent.id != category.id:
            category.parent_id = parent.id
    elif not data.ancestors:
        category.parent_id = None
    return category, created


def resolve_category_parents(session: Session, competitor_id: int) -> int:
    """Link categories whose parent was persisted after them (by the parent URL from navigation)."""
    result = session.execute(
        text(
            """
            UPDATE categories c SET parent_id = p.id
            FROM categories p
            WHERE c.competitor_id = :cid AND p.competitor_id = :cid
              AND p.url = c.details->>'parent_url' AND p.id <> c.id
              AND c.parent_id IS DISTINCT FROM p.id
            """
        ),
        {"cid": competitor_id},
    )
    return result.rowcount or 0


def category_subtree_ids(session: Session, competitor_id: int, root_id: int) -> list[int]:
    rows = session.execute(
        text(
            """
            WITH RECURSIVE tree AS (
                SELECT id FROM categories WHERE id = :root AND competitor_id = :cid
                UNION
                SELECT c.id FROM categories c JOIN tree t ON c.parent_id = t.id
            )
            SELECT id FROM tree
            """
        ),
        {"root": root_id, "cid": competitor_id},
    )
    return [r[0] for r in rows]


def category_url(session: Session, competitor_id: int, category_id: int) -> str:
    category = session.get(Category, category_id)
    if category is None or category.competitor_id != competitor_id:
        raise ValueError(f"category #{category_id} is not a category of this competitor")
    return category.url


@dataclass
class BannedScope:
    """URLs / external ids of a competitor's banned categories, as the crawls check them."""

    urls: set[str] = field(default_factory=set)
    external_ids: set[str] = field(default_factory=set)

    def covers(self, url: str | None, external_id: str | None, ancestor_urls: Iterable[str | None] = ()) -> bool:
        """The category is banned, or lies below a banned category."""
        return (
            url in self.urls
            or (external_id is not None and external_id in self.external_ids)
            or any(u in self.urls for u in ancestor_urls)
        )

    def covers_product(self, data: ProductData) -> bool:
        refs = [data.category, *data.breadcrumb]
        return any(ref is not None and self.covers(ref.url, ref.external_id) for ref in refs)


def banned_scope(session: Session, competitor_id: int) -> BannedScope:
    rows = session.execute(
        select(BannedCategory.url, BannedCategory.external_id).where(BannedCategory.competitor_id == competitor_id)
    ).all()
    return BannedScope({url for url, _ in rows}, {ext for _, ext in rows if ext})


def find_category_by_any(session: Session, competitor_id: int, key: str) -> Category | None:
    """Look a category up by internal id, external id or URL (CLI convenience)."""
    conditions = [Category.external_id == key, Category.url == key]
    if key.isdigit():
        conditions.append(Category.id == int(key))
    return session.scalar(select(Category).where(Category.competitor_id == competitor_id, or_(*conditions)))


# --------------------------------------------------------------------------- products


def product_identity_key(reference: str | None, url: str) -> str:
    """Stable product identity within a competitor.

    The catalog reference when the site exposes one; otherwise (documented fallback) the
    canonical product URL.
    """
    return f"ref:{reference}" if reference else f"url:{url}"


def upsert_product(
    session: Session,
    competitor_id: int,
    data: ProductData,
    *,
    category_id: int | None,
    run_id: int | None,
    now: datetime,
    mark_seen: bool = True,
) -> tuple[Product, bool]:
    """Insert or update a product; never creates a second row for the same identity.

    ``mark_seen=False`` updates details without touching catalog presence markers
    (used by ``price-refresh --update-details``).
    """
    identity = product_identity_key(data.catalog_reference, data.url)
    product = session.scalar(
        select(Product).where(Product.competitor_id == competitor_id, Product.identity_key == identity)
    )
    if product is None and data.catalog_reference:
        # A product first stored without reference (URL identity) now exposes one.
        product = session.scalar(
            select(Product).where(
                Product.competitor_id == competitor_id,
                Product.identity_key == product_identity_key(None, data.url),
            )
        )
        if product is not None:
            product.identity_key = identity
    if product is None and not data.catalog_reference:
        # Reference not readable this time: reuse the row already stored for this URL
        # rather than creating a URL-identity duplicate.
        product = session.scalar(
            select(Product).where(Product.competitor_id == competitor_id, Product.url == data.url).limit(1)
        )
    created = product is None
    if product is None:
        product = Product(competitor_id=competitor_id, identity_key=identity, url=data.url, first_seen_at=now)
        session.add(product)

    product.url = data.url
    _set_if_known(product, "catalog_reference", data.catalog_reference)
    _set_if_known(product, "external_id", data.external_id)
    _set_if_known(product, "title", data.title)
    _set_if_known(product, "description", data.description)
    _set_if_known(product, "long_description", data.long_description)
    _set_if_known(product, "brand", data.brand)
    _set_if_known(product, "attributes", data.attributes)
    _set_if_known(product, "normalized_attributes", data.normalized_attributes)
    _set_if_known(product, "images", data.images)
    if category_id is not None:
        product.category_id = category_id
    observed_extra = dict(data.extra)
    if data.breadcrumb:
        observed_extra["breadcrumb"] = [{"name": b.name, "url": b.url} for b in data.breadcrumb]
    if data.category:
        observed_extra["category_ref"] = {
            "url": data.category.url,
            "external_id": data.category.external_id,
            "name": data.category.name,
        }
    product.extra = {**(product.extra or {}), **{k: v for k, v in observed_extra.items() if v is not None}}
    if mark_seen or created:
        product.status = "active"
        product.missing_since = None
        product.last_seen_at = now
        product.last_seen_run_id = run_id
    product.details_updated_at = now
    session.flush()
    return product, created


def _set_if_known(obj: Any, field: str, value: Any) -> None:
    """``None`` means "not observed": keep what we already know."""
    if value is not None:
        setattr(obj, field, value)


def mark_unseen_missing(session: Session, competitor_id: int, run_id: int, now: datetime) -> tuple[int, int]:
    """After a complete, full-scope catalog run: flag what the run did not see."""
    cats = session.execute(
        update(Category)
        .where(
            Category.competitor_id == competitor_id,
            Category.status == "active",
            Category.last_seen_run_id.is_distinct_from(run_id),
        )
        .values(status="missing", missing_since=now)
    ).rowcount
    prods = session.execute(
        update(Product)
        .where(
            Product.competitor_id == competitor_id,
            Product.status == "active",
            Product.last_seen_run_id.is_distinct_from(run_id),
        )
        .values(status="missing", missing_since=now)
    ).rowcount
    return cats or 0, prods or 0


def count_unseen_active_products(session: Session, competitor_id: int, run_id: int) -> tuple[int, int]:
    total = session.scalar(
        select(func.count()).where(Product.competitor_id == competitor_id, Product.status == "active")
    )
    unseen = session.scalar(
        select(func.count()).where(
            Product.competitor_id == competitor_id,
            Product.status == "active",
            Product.last_seen_run_id.is_distinct_from(run_id),
        )
    )
    return unseen or 0, total or 0


def select_products_for_price_refresh(
    competitor_id: int,
    *,
    category_ids: Sequence[int] | None = None,
    references: Sequence[str] | None = None,
    product_ids: Sequence[int] | None = None,
    include_missing: bool = False,
    fresher_than: datetime | None = None,
    limit: int | None = None,
) -> Select[tuple[Product]]:
    stmt = select(Product).where(Product.competitor_id == competitor_id)
    if not include_missing:
        stmt = stmt.where(Product.status == "active")
    if category_ids is not None:
        stmt = stmt.where(Product.category_id.in_(category_ids))
    if references:
        stmt = stmt.where(Product.catalog_reference.in_(references))
    if product_ids is not None:
        stmt = stmt.where(Product.id.in_(product_ids))
    if fresher_than is not None:
        # skip products whose prices were observed after this instant
        stmt = stmt.where(or_(Product.last_price_observed_at.is_(None), Product.last_price_observed_at < fresher_than))
    stmt = stmt.order_by(Product.id)
    if limit:
        stmt = stmt.limit(limit)
    return stmt


# --------------------------------------------------------------------------- prices


def record_price_snapshot(
    session: Session,
    product: Product,
    snapshot: PriceSnapshot,
    *,
    run_id: int | None,
    observed_at: datetime,
) -> int:
    """Append every observed tier as a new observation (shared by catalog sync and price refresh).

    Nothing is written when the page showed no price: absence of a price is recorded on the
    run item, never as a fabricated observation.
    """
    if not snapshot.price_found or not snapshot.tiers:
        return 0
    for tier in snapshot.tiers:
        if not re.fullmatch(r"[A-Za-z]{3}", tier.currency or ""):
            raise ValueError(f"invalid currency {tier.currency!r}: not an ISO 4217 code")
    capture_id = uuid.uuid4()
    for tier in snapshot.tiers:
        session.add(
            ScrapeObservation(
                product_id=product.id,
                scrape_run_id=run_id,
                capture_id=capture_id,
                observed_at=observed_at,
                quantity=tier.quantity,
                min_quantity=tier.min_quantity,
                max_quantity=tier.max_quantity,
                max_inclusive=tier.max_inclusive,
                quantity_unit=tier.quantity_unit,
                price=tier.price,
                currency=tier.currency.upper(),
                price_basis=str(tier.price_basis),
                price_unit=tier.price_unit,
                price_unit_quantity=tier.price_unit_quantity,
                tax_mode=str(tier.tax_mode),
                is_promotional=tier.is_promotional,
                metadata_={**snapshot.metadata, **tier.metadata},
            )
        )
    product.last_price_observed_at = observed_at
    session.flush()
    return len(snapshot.tiers)


def latest_observations(session: Session, product_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        text("SELECT * FROM latest_price_observations WHERE product_id = :pid ORDER BY min_quantity NULLS FIRST, id"),
        {"pid": product_id},
    )
    return [dict(r._mapping) for r in rows]


def price_for_quantity(session: Session, product_id: int, quantity: Decimal) -> list[dict[str, Any]]:
    rows = session.execute(text("SELECT * FROM price_for_quantity(:pid, :qty)"), {"pid": product_id, "qty": quantity})
    return [dict(r._mapping) for r in rows]


def price_history(session: Session, product_id: int, quantity: Decimal | None = None) -> list[ScrapeObservation]:
    stmt = select(ScrapeObservation).where(ScrapeObservation.product_id == product_id)
    if quantity is not None:
        stmt = stmt.where(
            or_(ScrapeObservation.quantity.is_(None), ScrapeObservation.quantity == quantity),
            or_(ScrapeObservation.min_quantity.is_(None), ScrapeObservation.min_quantity <= quantity),
            or_(
                ScrapeObservation.max_quantity.is_(None),
                ScrapeObservation.max_quantity > quantity,
                and_(ScrapeObservation.max_inclusive, ScrapeObservation.max_quantity == quantity),
            ),
        )
    return list(session.scalars(stmt.order_by(ScrapeObservation.observed_at, ScrapeObservation.id)))


# --------------------------------------------------------------------------- runs


def failed_product_ids(session: Session, competitor_id: int, run_id: int) -> list[int]:
    """Products whose item failed in a run (by id, or by URL for catalog-run items whose
    product row could not be created/updated)."""
    failed = select(ScrapeRunItem.product_id, ScrapeRunItem.url).where(
        ScrapeRunItem.scrape_run_id == run_id,
        ScrapeRunItem.item_type == "product",
        ScrapeRunItem.status == "failed",
    )
    rows = session.execute(failed).all()
    ids = {pid for pid, _ in rows if pid is not None}
    urls = {url for pid, url in rows if pid is None}
    if urls:
        ids |= set(
            session.scalars(select(Product.id).where(Product.competitor_id == competitor_id, Product.url.in_(urls)))
        )
    return sorted(ids)


# --------------------------------------------------------------------------- listing page cache


def cached_listing_urls(session: Session, competitor_id: int, since: datetime) -> set[str]:
    return set(
        session.scalars(
            select(ListingPageCache.url).where(
                ListingPageCache.competitor_id == competitor_id, ListingPageCache.fetched_at >= since
            )
        )
    )


def load_listing_page(session: Session, competitor_id: int, url: str) -> ListingPageCache | None:
    return session.get(ListingPageCache, (competitor_id, url))


def store_listing_page(session: Session, competitor_id: int, run_id: int | None, page: FetchedPage) -> None:
    values = {
        "competitor_id": competitor_id,
        "url": page.requested_url,
        "final_url": page.final_url,
        "status_code": page.status_code,
        "fetched_at": page.fetched_at,
        "scrape_run_id": run_id,
        "body": zlib.compress(page.text.encode("utf-8"), 6),
    }
    stmt = pg_insert(ListingPageCache).values(values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["competitor_id", "url"],
            set_={k: stmt.excluded[k] for k in ("final_url", "status_code", "fetched_at", "scrape_run_id", "body")},
        )
    )


def purge_listing_cache(session: Session, competitor_id: int, older_than: datetime | None = None) -> int:
    stmt = delete(ListingPageCache).where(ListingPageCache.competitor_id == competitor_id)
    if older_than is not None:
        stmt = stmt.where(ListingPageCache.fetched_at < older_than)
    return session.execute(stmt).rowcount or 0


def failed_catalog_urls(session: Session, competitor_id: int, run_id: int) -> tuple[list[str], list[str]]:
    """(listing page URLs, product URLs) that failed in a catalog run of this competitor."""
    run = session.get(ScrapeRun, run_id)
    if run is None or run.competitor_id != competitor_id or run.operation != "catalog_discovery":
        raise ValueError(f"run #{run_id} is not a catalog run of this competitor")
    rows = session.execute(
        select(ScrapeRunItem.item_type, ScrapeRunItem.url)
        .where(ScrapeRunItem.scrape_run_id == run_id, ScrapeRunItem.status == "failed")
        .order_by(ScrapeRunItem.id)
    ).all()
    listings = list(dict.fromkeys(url for kind, url in rows if kind == "listing_page"))
    products = list(dict.fromkeys(url for kind, url in rows if kind == "product"))
    return listings, products


def count_failed_items(session: Session, run_id: int) -> dict[str, int]:
    """item_type -> number of failed items of a run."""
    return dict(
        session.execute(
            select(ScrapeRunItem.item_type, func.count())
            .where(ScrapeRunItem.scrape_run_id == run_id, ScrapeRunItem.status == "failed")
            .group_by(ScrapeRunItem.item_type)
        ).all()
    )


def recent_runs(session: Session, competitor_id: int | None = None, limit: int = 20) -> Iterable[ScrapeRun]:
    stmt = select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(limit)
    if competitor_id is not None:
        stmt = stmt.where(ScrapeRun.competitor_id == competitor_id)
    return session.scalars(stmt)
