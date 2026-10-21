from __future__ import annotations

from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from competitor_catalog.api import schemas, views
from competitor_catalog.api.deps import (
    ApiError,
    ConfigsDep,
    MatchingDep,
    PageDep,
    SessionDep,
    competitor_id_filter,
    competitor_or_404,
    not_found,
    require_api_key,
)
from competitor_catalog.api.routes.competitors import refusal
from competitor_catalog.core.urls import last_path_segment
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import BannedCategory, Category, Competitor, Product
from competitor_catalog.services import category_bans
from competitor_catalog.services import jobs as job_service

router = APIRouter(prefix="/categories", tags=["categories"], dependencies=[Depends(require_api_key)])


def _category(session, category_id: int) -> Category:
    category = session.get(Category, category_id)
    if category is None:
        raise not_found("category", category_id)
    return category


@router.get("", response_model=schemas.Page[schemas.CategoryOut], summary="List/search categories")
def list_categories(
    session: SessionDep,
    page: PageDep,
    competitor: Annotated[str | None, Query(description="competitor key")] = None,
    parent_id: Annotated[int | None, Query(description="direct children of this category")] = None,
    roots: Annotated[bool, Query(description="only categories without parent")] = False,
    status: schemas.LifecycleStatus | None = None,
    kind: str | None = None,
    q: Annotated[str | None, Query(description="search in name, external id, URL")] = None,
):
    stmt = select(Category).order_by(Category.depth.nulls_last(), Category.name, Category.id)
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(Category.competitor_id == competitor_id)
    if parent_id is not None:
        stmt = stmt.where(Category.parent_id == parent_id)
    if roots:
        stmt = stmt.where(Category.parent_id.is_(None))
    if status:
        stmt = stmt.where(Category.status == status)
    if kind:
        stmt = stmt.where(Category.kind == kind)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Category.name.ilike(like), Category.external_id.ilike(like), Category.url.ilike(like)))
    return page.page(session, stmt)


@router.get("/tree", response_model=list[schemas.CategoryTreeNode], summary="Whole category tree of a competitor")
def category_tree(
    session: SessionDep,
    competitor: Annotated[str, Query(description="competitor key")],
    include_missing: bool = False,
):
    """Nested tree with product counts (active products), sorted by name."""
    competitor_id = competitor_or_404(session, competitor).id
    stmt = select(Category).where(Category.competitor_id == competitor_id)
    if not include_missing:
        stmt = stmt.where(Category.status == "active")
    categories = list(session.scalars(stmt.order_by(Category.name)))
    counts = dict(
        session.execute(
            select(Product.category_id, func.count())
            .where(Product.competitor_id == competitor_id, Product.status == "active")
            .group_by(Product.category_id)
        ).all()
    )
    ids = {c.id for c in categories}
    children: dict[int | None, list[Category]] = defaultdict(list)
    for c in categories:
        children[c.parent_id if c.parent_id in ids else None].append(c)

    def build(category: Category, seen: frozenset[int]) -> schemas.CategoryTreeNode:
        nodes = [build(child, seen | {child.id}) for child in children[category.id] if child.id not in seen]
        own = counts.get(category.id, 0)
        return schemas.CategoryTreeNode(
            id=category.id,
            name=category.name,
            kind=category.kind,
            status=category.status,
            external_id=category.external_id,
            product_count=own,
            subtree_product_count=own + sum(n.subtree_product_count for n in nodes),
            children=nodes,
        )

    return [build(root, frozenset({root.id})) for root in children[None]]


# --------------------------------------------------------------------------- banned categories


def _ban(session, ban_id: int) -> BannedCategory:
    ban = session.get(BannedCategory, ban_id)
    if ban is None:
        raise not_found("banned category", ban_id)
    return ban


def _banned_out(session, bans: list[BannedCategory]) -> list[schemas.BannedCategoryOut]:
    keys = views.competitor_keys(session)
    parents = {
        (competitor_id, url): category_id
        for category_id, competitor_id, url in session.execute(
            select(Category.id, Category.competitor_id, Category.url).where(
                Category.url.in_({b.parent_url for b in bans if b.parent_url})
            )
        )
    }
    return [
        schemas.BannedCategoryOut.model_validate(
            {
                **{c.key: getattr(ban, c.key) for c in BannedCategory.__mapper__.column_attrs},
                "competitor_key": keys.get(ban.competitor_id, "?"),
                "parent_category_id": parents.get((ban.competitor_id, ban.parent_url)),
            }
        )
        for ban in bans
    ]


@router.get(
    "/banned", response_model=schemas.Page[schemas.BannedCategoryOut], summary="List banned categories, newest first"
)
def list_banned(
    session: SessionDep,
    page: PageDep,
    competitor: Annotated[str | None, Query(description="competitor key")] = None,
    q: Annotated[str | None, Query(description="search in name, external id, URL")] = None,
):
    stmt = select(BannedCategory).order_by(BannedCategory.banned_at.desc(), BannedCategory.id.desc())
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(BannedCategory.competitor_id == competitor_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(BannedCategory.name.ilike(like), BannedCategory.external_id.ilike(like), BannedCategory.url.ilike(like))
        )
    result = page.page(session, stmt)
    result["items"] = _banned_out(session, result["items"])
    return result


@router.get("/banned/{ban_id}", response_model=schemas.BannedCategoryOut, summary="One banned category")
def get_banned(ban_id: int, session: SessionDep):
    return _banned_out(session, [_ban(session, ban_id)])[0]


@router.post(
    "/banned/{ban_id}/unban",
    response_model=schemas.UnbanOut,
    summary="Unban a category: recreate it (empty), optionally queue its catalog sync",
    responses={409: {"model": schemas.ErrorResponse}},
)
def unban(
    ban_id: int,
    session: SessionDep,
    configs: ConfigsDep,
    matching: MatchingDep,
    body: Annotated[schemas.UnbanIn, Body(examples=[{}, {"scrape": True}])] = None,
):
    """The category is recreated with its saved metadata (name, URL, external id, kind, parent)
    under a **new id**, without sub-categories nor products: they come back with a catalog sync of
    the category (``scrape: true``, or ``POST /categories/{id}/scrape`` later) or the next full
    sync. With ``scrape: true`` the unban is refused as a whole when the sync cannot be queued
    (``worker_busy``, ``competitor_disabled``...)."""
    body = body or schemas.UnbanIn()
    ban = _ban(session, ban_id)
    competitor = session.get(Competitor, ban.competitor_id)
    category = category_bans.unban_category(session, ban, unbanned_by=body.unbanned_by)
    job = None
    if body.scrape:
        job = _enqueue(session, competitor, configs, matching, "catalog_sync", {"category_id": category.id})
    _commit(session)
    return schemas.UnbanOut(
        category=schemas.CategoryOut.model_validate(category), job=views.job_out(job, competitor.key) if job else None
    )


def _enqueue(session, competitor: Competitor, configs, matching, task: str, options: dict):
    config = configs.get(competitor.key)
    if config is None:
        session.rollback()
        raise ApiError(409, "competitor_not_configured", f"no configuration file for {competitor.key}")
    try:
        return job_service.enqueue_job(
            session, competitor, config, task, options, requested_by="api", matching=matching
        )
    except job_service.JobRequestError as exc:
        session.rollback()
        raise refusal(exc) from None


@router.get("/{category_id}", response_model=schemas.CategoryDetailOut, summary="One category with its path")
def get_category(category_id: int, session: SessionDep):
    category = _category(session, category_id)
    subtree = repo.category_subtree_ids(session, category.competitor_id, category.id)

    def active_products(*where):
        return session.scalar(select(func.count()).select_from(Product).where(Product.status == "active", *where))

    return {
        **schemas.CategoryOut.model_validate(category).model_dump(),
        "path": views.category_path(session, category.id),
        "children_count": session.scalar(
            select(func.count()).select_from(Category).where(Category.parent_id == category.id)
        ),
        "product_count": active_products(Product.category_id == category.id),
        "subtree_product_count": active_products(Product.category_id.in_(subtree)),
    }


@router.post("", response_model=schemas.CategoryOut, status_code=201, summary="Create a category")
def create_category(body: schemas.CategoryCreate, session: SessionDep):
    competitor = competitor_or_404(session, body.competitor)
    data = body.model_dump(exclude={"competitor"})
    data["slug"] = data["slug"] or last_path_segment(body.url)
    category = Category(competitor_id=competitor.id, **data)
    _check_parent(session, category, body.parent_id)
    session.add(category)
    _commit(session)
    return category


@router.patch("/{category_id}", response_model=schemas.CategoryOut, summary="Update a category")
def update_category(category_id: int, body: schemas.CategoryUpdate, session: SessionDep):
    category = _category(session, category_id)
    changes = body.model_dump(exclude_unset=True)
    if "parent_id" in changes:
        _check_parent(session, category, changes["parent_id"])
    for field, value in changes.items():
        setattr(category, field, value)
    _commit(session)
    return category


@router.delete("/{category_id}", status_code=204, summary="Delete a category")
def delete_category(category_id: int, session: SessionDep):
    """Its products and sub-categories are kept, detached (``category_id``/``parent_id`` set to null).
    A later catalog sync recreates it if it still exists on the site."""
    session.delete(_category(session, category_id))
    session.commit()
    return Response(status_code=204)


@router.post(
    "/{category_id}/ban",
    response_model=schemas.BannedCategoryOut,
    status_code=201,
    summary="Ban a category: delete it with its subtree and skip it in every crawl",
    responses={409: {"model": schemas.ErrorResponse}},
)
def ban(
    category_id: int,
    session: SessionDep,
    matching: MatchingDep,
    body: Annotated[schemas.BanIn, Body(examples=[{}, {"reason": "not our market", "banned_by": "alice"}])] = None,
):
    """Deletes the category, all its sub-categories, their products with their price history,
    their matches and match history. Matches of other competitors associated with a deleted
    (reference) product become ``deprecated``. Only the category's own metadata is kept (the
    returned ban), to recreate it with ``POST /categories/banned/{ban_id}/unban``. Catalog syncs
    skip the category, every page below it and every product whose navigation path contains it.

    Refusals (HTTP 409): ``worker_busy`` (the competitor has an active job), ``matching_running``
    (a category of the reference catalog while a matching job runs)."""
    body = body or schemas.BanIn()
    category = _category(session, category_id)
    try:
        banned = category_bans.ban_category(
            session,
            category,
            reason=body.reason,
            banned_by=body.banned_by,
            reference_key=matching.config.reference_competitor,
        )
    except category_bans.BanError as exc:
        session.rollback()
        raise ApiError(409, exc.code, exc.message, {"job_id": exc.job_id}) from None
    session.commit()
    return _banned_out(session, [banned])[0]


@router.post(
    "/{category_id}/scrape",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Catalog sync of this category only (sub-categories, products, prices)",
    responses={409: {"model": schemas.ErrorResponse}, 422: {"model": schemas.ErrorResponse}},
)
def scrape(
    category_id: int,
    session: SessionDep,
    configs: ConfigsDep,
    matching: MatchingDep,
    body: Annotated[schemas.CategorySyncIn, Body(examples=[{}, {"max_products": 10}])] = None,
):
    """Queues a ``catalog_sync`` job with ``options.category_id``: the crawl starts from the
    category's URL and follows its whole subtree, even when the category is empty in the
    database (e.g. just unbanned). Banned sub-categories stay skipped. Never a full sync: nothing
    is flagged missing. Same refusals as ``POST /competitors/{key}/worker/start``."""
    body = body or schemas.CategorySyncIn()
    category = _category(session, category_id)
    competitor = session.get(Competitor, category.competitor_id)
    options = {**body.model_dump(), "category_id": category.id}
    job = _enqueue(session, competitor, configs, matching, "catalog_sync", options)
    session.commit()
    return views.job_out(job, competitor.key)


@router.post(
    "/{category_id}/match",
    response_model=schemas.JobOut,
    status_code=202,
    summary="Match the products of this category only",
    responses={409: {"model": schemas.ErrorResponse}, 422: {"model": schemas.ErrorResponse}},
)
def match(
    category_id: int,
    session: SessionDep,
    configs: ConfigsDep,
    matching: MatchingDep,
    body: Annotated[schemas.CategoryMatchIn, Body(examples=[{}, {"mode": "all"}])] = None,
):
    """Queues a ``matching`` job with ``options.category_id``: only the competitor's products
    of this category and its sub-categories are (re)matched, against the whole reference
    catalog. Same refusals as ``POST /matching/competitors/{key}/run`` (a category of the
    reference catalog itself is refused with ``matching_reference``)."""
    body = body or schemas.CategoryMatchIn()
    category = _category(session, category_id)
    competitor = session.get(Competitor, category.competitor_id)
    options = {**body.model_dump(), "category_id": category.id}
    job = _enqueue(session, competitor, configs, matching, "matching", options)
    session.commit()
    return views.job_out(job, competitor.key)


def _check_parent(session, category: Category, parent_id: int | None) -> None:
    if parent_id is None:
        return
    parent = session.get(Category, parent_id)
    if parent is None or parent.competitor_id != category.competitor_id:
        raise ApiError(422, "invalid_parent", f"parent category {parent_id} not found for this competitor")
    if category.id is not None and category.id in {c.id for c in views.category_path(session, parent_id)}:
        raise ApiError(422, "invalid_parent", "a category cannot be moved below itself")


def _commit(session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ApiError(
            409, "conflict", "a category with the same URL or external id already exists", str(exc.orig)[:300]
        ) from None
