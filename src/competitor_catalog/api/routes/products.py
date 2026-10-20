"""Products: search, facets, CRUD, prices."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import delete, func, or_, select, true
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
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import Category, Product, ProductMatch, ScrapeObservation
from competitor_catalog.matching import service as matching_service

router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(require_api_key)])

SortKey = Literal[
    "title", "reference", "brand", "created_at", "updated_at", "last_seen_at", "last_price_observed_at", "id"
]
SORTS = {
    "title": Product.title,
    "reference": Product.catalog_reference,
    "brand": Product.brand,
    "created_at": Product.created_at,
    "updated_at": Product.updated_at,
    "last_seen_at": Product.last_seen_at,
    "last_price_observed_at": Product.last_price_observed_at,
    "id": Product.id,
}


def _product(session, product_id: int) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise not_found("product", product_id)
    return product


def _attr_value(raw: str) -> Any:
    try:
        return json.loads(raw)  # numbers, booleans, quoted strings
    except ValueError:
        return raw


def _filtered(
    session,
    *,
    competitor: str | None,
    category_id: int | None,
    include_subcategories: bool,
    q: str | None,
    status: str | None,
    brand: list[str] | None,
    reference: list[str] | None,
    has_price: bool | None,
    attr: list[str] | None,
):
    stmt = select(Product)
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(Product.competitor_id == competitor_id)
    if category_id is not None:
        category = session.get(Category, category_id)
        if category is None:
            raise not_found("category", category_id)
        ids = (
            repo.category_subtree_ids(session, category.competitor_id, category_id)
            if include_subcategories
            else [category_id]
        )
        stmt = stmt.where(Product.category_id.in_(ids))
    if q:
        for word in q.split():
            like = f"%{word}%"
            stmt = stmt.where(
                or_(
                    Product.title.ilike(like),
                    Product.catalog_reference.ilike(like),
                    Product.brand.ilike(like),
                    Product.description.ilike(like),
                )
            )
    if status:
        stmt = stmt.where(Product.status == status)
    if brand:
        stmt = stmt.where(Product.brand.in_(brand))
    if reference:
        stmt = stmt.where(Product.catalog_reference.in_(reference))
    if has_price is not None:
        exists = select(ScrapeObservation.id).where(ScrapeObservation.product_id == Product.id).exists()
        stmt = stmt.where(exists if has_price else ~exists)
    for item in attr or []:
        key, sep, raw = item.partition(":")
        if not sep or not key:
            raise ApiError(422, "invalid_filter", f"attr filter {item!r} must be 'key:value'")
        stmt = stmt.where(Product.normalized_attributes.contains({key: _attr_value(raw)}))
    return stmt


@router.get("", response_model=schemas.Page[schemas.ProductListItem], summary="Search products")
def list_products(
    session: SessionDep,
    page: PageDep,
    competitor: Annotated[str | None, Query(description="competitor key")] = None,
    category_id: int | None = None,
    include_subcategories: Annotated[bool, Query(description="with category_id: whole subtree")] = True,
    q: Annotated[str | None, Query(description="words searched in title, references, brand, description")] = None,
    status: schemas.LifecycleStatus | None = None,
    brand: Annotated[list[str] | None, Query(description="repeatable")] = None,
    reference: Annotated[list[str] | None, Query(description="catalog reference, repeatable")] = None,
    has_price: bool | None = None,
    attr: Annotated[
        list[str] | None,
        Query(description="normalized attribute filter 'key:value', repeatable (e.g. length_mm:300, color:brun)"),
    ] = None,
    sort: SortKey = "title",
    order: Literal["asc", "desc"] = "asc",
):
    stmt = _filtered(
        session,
        competitor=competitor,
        category_id=category_id,
        include_subcategories=include_subcategories,
        q=q,
        status=status,
        brand=brand,
        reference=reference,
        has_price=has_price,
        attr=attr,
    )
    column = SORTS[sort]
    stmt = stmt.order_by(column.desc().nulls_last() if order == "desc" else column.asc().nulls_last(), Product.id)
    result = page.page(session, stmt)
    ids = [p.id for p in result["items"]]
    prices = views.price_summaries(session, ids)
    trends = views.price_trends(session, ids)
    result["items"] = [
        schemas.ProductListItem.model_validate(
            {**_columns(p), "latest_price": prices.get(p.id), "price_trend": trends.get(p.id)}
        )
        for p in result["items"]
    ]
    return result


def _columns(product: Product) -> dict[str, Any]:
    return {c.key: getattr(product, c.key) for c in Product.__mapper__.column_attrs if c.key != "antalis_reference"}


@router.get("/facets", response_model=schemas.ProductFacets, summary="Filter values for the current search")
def product_facets(
    session: SessionDep,
    competitor: str | None = None,
    category_id: int | None = None,
    include_subcategories: bool = True,
    q: str | None = None,
    status: schemas.LifecycleStatus | None = None,
    brand: Annotated[list[str] | None, Query()] = None,
    attr: Annotated[list[str] | None, Query()] = None,
    values_per_attribute: Annotated[int, Query(ge=1, le=200)] = 30,
):
    """Brands, statuses and normalized attribute values (with counts) of the products
    matching the same filters as ``GET /products``."""
    stmt = _filtered(
        session,
        competitor=competitor,
        category_id=category_id,
        include_subcategories=include_subcategories,
        q=q,
        status=status,
        brand=brand,
        reference=None,
        has_price=None,
        attr=attr,
    )
    ids = stmt.with_only_columns(Product.id).subquery()
    total = session.scalar(select(func.count()).select_from(ids))

    def counts(column):
        rows = session.execute(
            select(column, func.count())
            .where(Product.id.in_(select(ids.c.id)))
            .group_by(column)
            .order_by(func.count().desc())
        ).all()
        return [schemas.FacetValue(value=v, count=n) for v, n in rows if v is not None]

    kv = func.jsonb_each(Product.normalized_attributes).table_valued("key", "value").lateral("kv")
    attr_rows = session.execute(
        select(kv.c.key, kv.c.value, func.count().label("n"))
        .select_from(Product)
        .join(kv, true())
        .where(Product.id.in_(select(ids.c.id)), func.jsonb_typeof(Product.normalized_attributes) == "object")
        .group_by(kv.c.key, kv.c.value)
        .order_by(kv.c.key, func.count().desc())
    ).all()
    attributes: dict[str, list[schemas.FacetValue]] = {}
    for key, value, n in attr_rows:
        values = attributes.setdefault(key, [])
        if len(values) < values_per_attribute:
            values.append(schemas.FacetValue(value=value, count=n))
    return schemas.ProductFacets(
        total=total, brands=counts(Product.brand), statuses=counts(Product.status), attributes=attributes
    )


@router.get(
    "/by-reference/{competitor}/{reference}",
    response_model=schemas.ProductDetailOut,
    summary="One product by competitor key and catalog reference",
)
def get_by_reference(competitor: str, reference: str, session: SessionDep):
    competitor_id = competitor_or_404(session, competitor).id
    product = session.scalar(
        select(Product).where(Product.competitor_id == competitor_id, Product.catalog_reference == reference)
    )
    if product is None:
        raise not_found("product", reference)
    return _detail(session, product)


@router.get("/{product_id}", response_model=schemas.ProductDetailOut, summary="One product with latest prices")
def get_product(product_id: int, session: SessionDep):
    return _detail(session, _product(session, product_id))


def _detail(session, product: Product) -> dict[str, Any]:
    prices = views.price_summaries(session, [product.id])
    associated = ProductMatch.status.in_(("auto_matched", "validated"))
    matches = session.scalar(
        select(func.count())
        .select_from(ProductMatch)
        .where(associated, or_(ProductMatch.antalis_product_id == product.id, ProductMatch.product_id == product.id))
    )
    match = session.scalar(select(ProductMatch).where(ProductMatch.product_id == product.id))
    return {
        **_columns(product),
        "latest_price": prices.get(product.id),
        "price_trend": views.price_trends(session, [product.id]).get(product.id),
        "category_path": views.category_path(session, product.category_id),
        "latest_prices": repo.latest_observations(session, product.id),
        "match_count": matches,
        "matching": schemas.MatchBriefOut(
            match_id=match.id,
            status=match.status,
            antalis_product_id=match.antalis_product_id,
            proposed_antalis_product_id=match.proposed_antalis_product_id,
            score=float(match.score) if match.score is not None else None,
        )
        if match
        else None,
    }


@router.post("", response_model=schemas.ProductDetailOut, status_code=201, summary="Create a product")
def create_product(body: schemas.ProductCreate, session: SessionDep):
    competitor = competitor_or_404(session, body.competitor)
    data = body.model_dump(exclude={"competitor"})
    _check_category(session, competitor.id, data.get("category_id"))
    product = Product(
        competitor_id=competitor.id,
        identity_key=repo.product_identity_key(body.catalog_reference, body.url),
        **data,
    )
    session.add(product)
    _commit(session)
    return _detail(session, product)


@router.patch("/{product_id}", response_model=schemas.ProductDetailOut, summary="Update a product")
def update_product(product_id: int, body: schemas.ProductUpdate, session: SessionDep):
    product = _product(session, product_id)
    changes = body.model_dump(exclude_unset=True)
    if "category_id" in changes:
        _check_category(session, product.competitor_id, changes["category_id"])
    for field, value in changes.items():
        setattr(product, field, value)
    if {"catalog_reference", "url"} & set(changes):
        product.identity_key = repo.product_identity_key(product.catalog_reference, product.url)
    _commit(session)
    # matches computed from the former values become deprecated (with a notification)
    matching_service.detect_stale_matches(session, product_ids=[product.id])
    session.commit()
    return _detail(session, product)


@router.delete("/{product_id}", status_code=204, summary="Delete a product and its price history")
def delete_product(product_id: int, session: SessionDep):
    """Deletes its price observations and its own match too; the matches associated with it
    (a reference product) become deprecated. A later catalog sync recreates the product if
    it is still listed on the site."""
    product = _product(session, product_id)
    watching = list(
        session.scalars(
            select(ProductMatch.product_id).where(
                or_(
                    ProductMatch.antalis_product_id == product.id,
                    ProductMatch.proposed_antalis_product_id == product.id,
                )
            )
        )
    )
    session.execute(delete(ScrapeObservation).where(ScrapeObservation.product_id == product.id))
    session.delete(product)
    session.commit()
    if watching:
        matching_service.detect_stale_matches(session, product_ids=watching)
        session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- prices


@router.get("/{product_id}/prices", response_model=list[schemas.ObservationOut], summary="Latest captured price tiers")
def latest_prices(product_id: int, session: SessionDep):
    _product(session, product_id)
    return repo.latest_observations(session, product_id)


@router.get(
    "/{product_id}/prices/history",
    response_model=list[schemas.ObservationOut],
    summary="Every observed price, oldest first",
)
def price_history(
    product_id: int,
    session: SessionDep,
    quantity: Annotated[Decimal | None, Query(description="only the tiers that apply to this quantity")] = None,
):
    _product(session, product_id)
    return repo.price_history(session, product_id, quantity)


@router.get(
    "/{product_id}/prices/for-quantity",
    response_model=list[schemas.ObservationOut],
    summary="Latest price applicable to a quantity (degressive tiers)",
)
def price_for_quantity(product_id: int, quantity: Annotated[Decimal, Query(gt=0)], session: SessionDep):
    _product(session, product_id)
    return repo.price_for_quantity(session, product_id, quantity)


def _check_category(session, competitor_id: int, category_id: int | None) -> None:
    if category_id is None:
        return
    category = session.get(Category, category_id)
    if category is None or category.competitor_id != competitor_id:
        raise ApiError(422, "invalid_category", f"category {category_id} not found for this competitor")


def _commit(session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ApiError(
            409, "conflict", "a product with the same identity (reference or URL) already exists", str(exc.orig)[:300]
        ) from None
