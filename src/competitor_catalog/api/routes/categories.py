from __future__ import annotations

from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from competitor_catalog.api import schemas, views
from competitor_catalog.api.deps import (
    ApiError,
    PageDep,
    SessionDep,
    competitor_id_filter,
    competitor_or_404,
    not_found,
    require_api_key,
)
from competitor_catalog.core.urls import last_path_segment
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import Category, Product

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
