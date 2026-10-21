"""Banning a category: it is deleted with its whole subtree (sub-categories, products, price
history, matches) and excluded from every later crawl. Only its own metadata is kept, in
``banned_categories``, so that unbanning recreates it (empty, same URL / external id / parent)
and a catalog sync of that category (``category_id`` option) can fill it again.

Matches of other competitors associated with (or proposing) a deleted reference product
become ``deprecated`` (``antalis_product_deleted``), as when a product is deleted.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from competitor_catalog.core.types import CategoryRef
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import (
    JOB_ACTIVE_STATUSES,
    BannedCategory,
    Category,
    Competitor,
    Product,
    ProductMatch,
    ScrapeObservation,
    WorkerJob,
)
from competitor_catalog.matching.service import detect_stale_matches
from competitor_catalog.services.notifications import notify
from competitor_catalog.services.runs import utcnow


class BanError(Exception):
    """A ban the API must refuse; ``code`` is stable and documented."""

    def __init__(self, code: str, message: str, *, job_id: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.job_id = job_id


def _check_no_job(session: Session, competitor: Competitor, reference_key: str | None) -> None:
    """A running job could recreate or read the rows being deleted: bans wait for it."""
    busy = session.scalar(
        select(WorkerJob).where(WorkerJob.competitor_id == competitor.id, WorkerJob.status.in_(JOB_ACTIVE_STATUSES))
    )
    if busy is not None:
        raise BanError(
            "worker_busy",
            f"{competitor.key} has a {busy.status} {busy.task} job (#{busy.id}): stop it or wait for it first",
            job_id=busy.id,
        )
    if competitor.key == reference_key:
        matching = session.scalar(
            select(WorkerJob).where(WorkerJob.task == "matching", WorkerJob.status.in_(JOB_ACTIVE_STATUSES))
        )
        if matching is not None:
            raise BanError(
                "matching_running",
                f"a matching job (#{matching.id}) reads the reference catalog: stop it or wait for it first",
                job_id=matching.id,
            )


def ban_category(
    session: Session,
    category: Category,
    *,
    reason: str | None = None,
    banned_by: str | None = None,
    reference_key: str | None = None,
) -> BannedCategory:
    """Delete the category with everything below it and remember it as banned (flushes, no commit)."""
    competitor = session.get(Competitor, category.competitor_id)
    _check_no_job(session, competitor, reference_key)
    subtree = repo.category_subtree_ids(session, competitor.id, category.id)
    products = select(Product.id).where(Product.category_id.in_(subtree))
    # matches of other products that depend on a product deleted here (as reference product)
    watching = list(
        session.scalars(
            select(ProductMatch.product_id).where(
                or_(
                    ProductMatch.antalis_product_id.in_(products),
                    ProductMatch.proposed_antalis_product_id.in_(products),
                ),
                ProductMatch.product_id.not_in(products),
            )
        )
    )
    deleted: dict[str, Any] = {
        "categories": len(subtree),
        "products": session.scalar(select(func.count()).select_from(products.subquery())),
        "matches": session.scalar(
            select(func.count()).select_from(ProductMatch).where(ProductMatch.product_id.in_(products))
        ),
    }
    ban = BannedCategory(
        competitor_id=competitor.id,
        category_id=category.id,
        name=category.name,
        slug=category.slug,
        url=category.url,
        external_id=category.external_id,
        kind=category.kind,
        depth=category.depth,
        breadcrumb=category.breadcrumb,
        details=dict(category.details or {}),
        parent_url=_parent_url(session, category),
        first_seen_at=category.first_seen_at,
        reason=reason,
        banned_by=banned_by,
        banned_at=utcnow(),
    )
    deleted["observations"] = (
        session.execute(delete(ScrapeObservation).where(ScrapeObservation.product_id.in_(products))).rowcount or 0
    )
    # matches, match history, candidates and embeddings of these products cascade in the database
    session.execute(
        delete(Product).where(Product.category_id.in_(subtree)), execution_options={"synchronize_session": False}
    )
    session.execute(delete(Category).where(Category.id.in_(subtree)), execution_options={"synchronize_session": False})
    session.expire_all()  # rows changed by the database cascades (SET NULL on matches)
    deprecations = detect_stale_matches(session, product_ids=watching) if watching else []
    deleted["matches_deprecated"] = len(deprecations)
    ban.deleted = deleted
    # an older ban of the same URL (the category was recreated by hand since) is replaced
    session.execute(
        delete(BannedCategory).where(BannedCategory.competitor_id == competitor.id, BannedCategory.url == ban.url)
    )
    session.add(ban)
    session.flush()
    notify(
        session,
        level="warning",
        kind="category_banned",
        title=f"Category banned: {ban.name} ({competitor.name})",
        message=f"Deleted {deleted['categories']} categor{'y' if deleted['categories'] == 1 else 'ies'}, "
        f"{deleted['products']} product(s), {deleted['observations']} price observation(s), "
        f"{deleted['matches']} match(es); {deleted['matches_deprecated']} match(es) of other competitors "
        "deprecated. Catalog syncs skip it until it is unbanned.",
        competitor_id=competitor.id,
        data={"ban_id": ban.id, "category_id": ban.category_id, "url": ban.url, "deleted": deleted},
    )
    return ban


def _parent_url(session: Session, category: Category) -> str | None:
    if category.parent_id is not None:
        parent = session.get(Category, category.parent_id)
        if parent is not None:
            return parent.url
    return (category.details or {}).get("parent_url")


def unban_category(session: Session, ban: BannedCategory, *, unbanned_by: str | None = None) -> Category:
    """Recreate the category (empty) from the banned metadata and forget the ban (flushes, no commit).

    Its sub-categories and products come back with the next catalog sync of the category (or a
    full one)."""
    competitor = session.get(Competitor, ban.competitor_id)
    category = repo.find_category(session, ban.competitor_id, CategoryRef(url=ban.url, external_id=ban.external_id))
    if category is None:
        now = utcnow()
        category = Category(
            competitor_id=ban.competitor_id,
            name=ban.name,
            slug=ban.slug,
            url=ban.url,
            external_id=ban.external_id,
            kind=ban.kind,
            depth=ban.depth,
            breadcrumb=ban.breadcrumb,
            details=dict(ban.details or {}),
            status="active",
            first_seen_at=ban.first_seen_at or now,
            last_seen_at=now,
        )
        session.add(category)
    if ban.parent_url:
        parent = session.scalar(
            select(Category).where(Category.competitor_id == ban.competitor_id, Category.url == ban.parent_url)
        )
        if parent is not None and parent is not category:
            category.parent_id = parent.id
    session.delete(ban)
    session.flush()
    notify(
        session,
        level="info",
        kind="category_unbanned",
        title=f"Category unbanned: {category.name} ({competitor.name})",
        message="Recreated empty: a catalog sync of this category (or a full one) fills it again."
        + (f" Unbanned by {unbanned_by}." if unbanned_by else ""),
        competitor_id=competitor.id,
        data={"category_id": category.id, "url": category.url},
    )
    return category
