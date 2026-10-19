"""Price observations (append-only history written by the scrapers; editable for corrections)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select

from competitor_catalog.api import schemas
from competitor_catalog.api.deps import ApiError, PageDep, SessionDep, competitor_id_filter, not_found, require_api_key
from competitor_catalog.db.models import Product, ScrapeObservation
from competitor_catalog.services.runs import utcnow

router = APIRouter(prefix="/observations", tags=["price observations"], dependencies=[Depends(require_api_key)])


def _observation(session, observation_id: int) -> ScrapeObservation:
    observation = session.get(ScrapeObservation, observation_id)
    if observation is None:
        raise not_found("observation", observation_id)
    return observation


@router.get("", response_model=schemas.Page[schemas.ObservationOut], summary="List observations, newest first")
def list_observations(
    session: SessionDep,
    page: PageDep,
    product_id: int | None = None,
    competitor: Annotated[str | None, Query(description="competitor key")] = None,
    run_id: int | None = None,
    capture_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
):
    stmt = select(ScrapeObservation).order_by(ScrapeObservation.observed_at.desc(), ScrapeObservation.id)
    if product_id is not None:
        stmt = stmt.where(ScrapeObservation.product_id == product_id)
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.join(Product, Product.id == ScrapeObservation.product_id).where(
            Product.competitor_id == competitor_id
        )
    if run_id is not None:
        stmt = stmt.where(ScrapeObservation.scrape_run_id == run_id)
    if capture_id is not None:
        stmt = stmt.where(ScrapeObservation.capture_id == capture_id)
    if since is not None:
        stmt = stmt.where(ScrapeObservation.observed_at >= since)
    if until is not None:
        stmt = stmt.where(ScrapeObservation.observed_at < until)
    return page.page(session, stmt)


@router.get("/{observation_id}", response_model=schemas.ObservationOut, summary="One observation")
def get_observation(observation_id: int, session: SessionDep):
    return _observation(session, observation_id)


@router.post("", response_model=schemas.ObservationOut, status_code=201, summary="Add an observation (manual price)")
def create_observation(body: schemas.ObservationCreate, session: SessionDep):
    product = session.get(Product, body.product_id)
    if product is None:
        raise ApiError(422, "invalid_product", f"product {body.product_id} not found")
    data = body.model_dump()
    metadata = data.pop("metadata")
    observation = ScrapeObservation(
        **{**data, "observed_at": body.observed_at or utcnow(), "capture_id": body.capture_id or uuid.uuid4()},
        metadata_={"source": "api", **metadata},
    )
    session.add(observation)
    session.flush()
    _sync_last_price(session, product)
    session.commit()
    return observation


@router.patch("/{observation_id}", response_model=schemas.ObservationOut, summary="Correct an observation")
def update_observation(observation_id: int, body: schemas.ObservationUpdate, session: SessionDep):
    observation = _observation(session, observation_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(observation, "metadata_" if field == "metadata" else field, value)
    session.flush()
    _sync_last_price(session, session.get(Product, observation.product_id))
    session.commit()
    return observation


@router.delete("/{observation_id}", status_code=204, summary="Delete an observation")
def delete_observation(observation_id: int, session: SessionDep):
    observation = _observation(session, observation_id)
    product = session.get(Product, observation.product_id)
    session.delete(observation)
    session.flush()
    _sync_last_price(session, product)
    session.commit()
    return Response(status_code=204)


def _sync_last_price(session, product: Product) -> None:
    product.last_price_observed_at = session.scalar(
        select(func.max(ScrapeObservation.observed_at)).where(ScrapeObservation.product_id == product.id)
    )
