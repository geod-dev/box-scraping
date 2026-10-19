"""Product matches: equivalences between products of different competitors (never merges)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from competitor_catalog.api import schemas
from competitor_catalog.api.deps import ApiError, PageDep, SessionDep, not_found, require_api_key
from competitor_catalog.db.models import Product, ProductMatch
from competitor_catalog.services.runs import utcnow

router = APIRouter(prefix="/product-matches", tags=["product matches"], dependencies=[Depends(require_api_key)])


def _match(session, match_id: int) -> ProductMatch:
    match = session.get(ProductMatch, match_id)
    if match is None:
        raise not_found("product match", match_id)
    return match


@router.get("", response_model=schemas.Page[schemas.MatchOut], summary="List matches")
def list_matches(
    session: SessionDep,
    page: PageDep,
    product_id: int | None = None,
    status: schemas.MatchStatus | None = None,
):
    stmt = select(ProductMatch).order_by(ProductMatch.id.desc())
    if product_id is not None:
        stmt = stmt.where(or_(ProductMatch.product_a_id == product_id, ProductMatch.product_b_id == product_id))
    if status:
        stmt = stmt.where(ProductMatch.status == status)
    return page.page(session, stmt)


@router.get("/{match_id}", response_model=schemas.MatchOut, summary="One match")
def get_match(match_id: int, session: SessionDep):
    return _match(session, match_id)


@router.post("", response_model=schemas.MatchOut, status_code=201, summary="Create a match")
def create_match(body: schemas.MatchCreate, session: SessionDep):
    """The pair is stored ordered (``product_a_id < product_b_id``) whatever the order given."""
    a, b = sorted((body.product_a_id, body.product_b_id))
    if a == b:
        raise ApiError(422, "invalid_match", "a product cannot match itself")
    products = {p.id: p for p in session.scalars(select(Product).where(Product.id.in_((a, b))))}
    if len(products) != 2:
        raise ApiError(422, "invalid_match", "both products must exist")
    if products[a].competitor_id == products[b].competitor_id:
        raise ApiError(422, "invalid_match", "a match links products of two different competitors")
    data = body.model_dump()
    data.update(product_a_id=a, product_b_id=b)
    match = ProductMatch(**data)
    if match.status == "validated":
        match.validated_at = utcnow()
    session.add(match)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ApiError(409, "conflict", "these two products are already matched") from None
    return match


@router.patch("/{match_id}", response_model=schemas.MatchOut, summary="Update a match (e.g. validate it)")
def update_match(match_id: int, body: schemas.MatchUpdate, session: SessionDep):
    match = _match(session, match_id)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(match, field, value)
    if changes.get("status") == "validated":
        match.validated_at = utcnow()
    session.commit()
    return match


@router.delete("/{match_id}", status_code=204, summary="Delete a match")
def delete_match(match_id: int, session: SessionDep):
    session.delete(_match(session, match_id))
    session.commit()
    return Response(status_code=204)
