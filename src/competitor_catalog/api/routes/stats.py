"""Activity statistics for the console dashboard."""

from __future__ import annotations

import statistics
import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Date, cast, func, select, text
from sqlalchemy.orm import Session, aliased

from competitor_catalog.api import schemas, views
from competitor_catalog.api.deps import ApiError, MatchingDep, SessionDep, require_api_key
from competitor_catalog.db.models import (
    Competitor,
    Product,
    ProductMatch,
    ProductMatchEvent,
    ScrapeObservation,
    ScrapeRun,
    WorkerJob,
)

router = APIRouter(prefix="/stats", tags=["system"], dependencies=[Depends(require_api_key)])

CREDITS_CACHE_SECONDS = 60  # Scrape.do allows 10 calls/min on its info endpoint
SAME_PRICE_PERCENT = 1


@router.get("/overview", response_model=schemas.StatsOverview, summary="Daily activity, credits, price position")
def overview(
    request: Request,
    session: SessionDep,
    matching: MatchingDep,
    days: Annotated[int, Query(ge=1, le=366, description="period length, today included")] = 30,
    tz: Annotated[str, Query(description="time zone of the days (e.g. Europe/Paris)")] = "UTC",
) -> schemas.StatsOverview:
    """``daily``: one row per day of the period (zeros included). ``price_position``: per
    non-reference competitor, over the associated pairs (auto_matched or validated, both
    products active) whose latest from prices are comparable: same currency, both the price of
    one sales unit (unit basis, unit quantity 1 or unknown), tax modes not known to differ.
    gap = (competitor - antalis) / antalis * 100; ``same`` = |gap| < 1."""
    if not session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = :tz)"), {"tz": tz}):
        raise ApiError(422, "invalid_time_zone", f"unknown time zone {tz!r}")
    today: date = session.scalar(text("SELECT (now() AT TIME ZONE :tz)::date"), {"tz": tz})
    first = today - timedelta(days=days - 1)
    start = session.scalar(text("SELECT CAST(:day AS timestamp) AT TIME ZONE :tz"), {"day": first, "tz": tz})
    rows = {first + timedelta(days=i): _empty_day(first + timedelta(days=i)) for i in range(days)}

    def per_day(column, *where, value=None, field: str) -> None:
        day = cast(func.timezone(tz, column), Date)
        stmt = select(day, value if value is not None else func.count()).where(column >= start, *where).group_by(day)
        for d, n in session.execute(stmt).all():
            if d in rows:
                rows[d][field] = int(n or 0)

    per_day(ScrapeObservation.observed_at, field="observations")
    per_day(Product.first_seen_at, field="products_created")
    per_day(
        ScrapeRun.finished_at,
        ScrapeRun.operation == "catalog_discovery",
        value=func.sum(ScrapeRun.products_marked_missing),
        field="products_marked_missing",
    )
    per_day(ScrapeRun.started_at, value=func.sum(ScrapeRun.credits_used), field="credits_used")
    per_day(WorkerJob.finished_at, WorkerJob.status == "succeeded", field="jobs_succeeded")
    per_day(WorkerJob.finished_at, WorkerJob.status == "failed", field="jobs_failed")
    per_day(
        ProductMatchEvent.created_at,
        ProductMatchEvent.event.in_(("computed", "recomputed")),
        ProductMatchEvent.to_status == "auto_matched",
        field="matches_auto",
    )
    per_day(ProductMatchEvent.created_at, ProductMatchEvent.event == "validated", field="matches_validated")
    per_day(ProductMatchEvent.created_at, ProductMatchEvent.event == "rejected", field="matches_rejected")

    increases = decreases = 0
    changes = text(
        "SELECT CAST(timezone(:tz, observed_at) AS date) AS day, price > previous_price AS up, count(*) AS n "
        f"FROM ({_changes_since()}) changes WHERE observed_at >= :start GROUP BY 1, 2"
    )
    for d, up, n in session.execute(changes, {"tz": tz, "start": start}).all():
        if d in rows:
            rows[d]["price_changes"] += n
        increases += n if up else 0
        decreases += 0 if up else n

    jobs = dict(
        session.execute(
            select(WorkerJob.status, func.count())
            .where(WorkerJob.finished_at >= start, WorkerJob.status.in_(tuple(schemas.JobStatusTotals.model_fields)))
            .group_by(WorkerJob.status)
        ).all()
    )
    daily = [schemas.DailyStats(**row) for row in rows.values()]
    return schemas.StatsOverview(
        days=days,
        daily=daily,
        totals=schemas.StatsTotals(
            observations=sum(d.observations for d in daily),
            products_created=sum(d.products_created for d in daily),
            credits_used=sum(d.credits_used for d in daily),
            price_increases=increases,
            price_decreases=decreases,
            jobs=schemas.JobStatusTotals(**jobs),
        ),
        credits=scrapedo_credits(request),
        price_position=price_position(session, matching.config.reference_competitor),
    )


def _empty_day(day: date) -> dict[str, Any]:
    fields = (f for f in schemas.DailyStats.model_fields if f != "date")
    return {"date": day.isoformat(), **{f: 0 for f in fields}}


def _changes_since() -> str:
    # captures of the products priced during the period, with their whole history (the
    # previous capture of the first change may be older than the period)
    where = "WHERE o.product_id IN (SELECT product_id FROM scrape_observations WHERE observed_at >= :start)"
    return views.FROM_PRICE_CHANGES.format(where=where)


# --------------------------------------------------------------------------- credits

_credits_lock = threading.Lock()


def scrapedo_credits(request: Request) -> schemas.CreditsOut:
    """Scrape.do quota, cached CREDITS_CACHE_SECONDS per API process (failures too)."""
    state = request.app.state
    with _credits_lock:
        cached = getattr(state, "scrapedo_credits", None)
        if cached is not None and time.monotonic() - cached[0] < CREDITS_CACHE_SECONDS:
            return cached[1]
        credits = _fetch_credits(state.settings.scrapedo_api_key)
        state.scrapedo_credits = (time.monotonic(), credits)
        return credits


def _fetch_credits(key: str | None) -> schemas.CreditsOut:
    from competitor_catalog.http.scrapedo import ScrapeDoClient

    empty = {"monthly_limit": None, "monthly_remaining": None, "renews_at": None}
    if not key:
        return schemas.CreditsOut(**empty, error="SCRAPEDO_API_KEY is not set on the API service")
    try:
        with ScrapeDoClient(key, max_attempts=1) as client:
            info = client.account_info()
    except Exception as exc:
        return schemas.CreditsOut(**empty, error=f"Scrape.do account endpoint failed: {type(exc).__name__}")
    return schemas.CreditsOut(
        monthly_limit=info.get("MaxMonthlyRequest"),
        monthly_remaining=info.get("RemainingMonthlyRequest"),
        renews_at=None,
    )


# --------------------------------------------------------------------------- price position

LATEST_FROM_PRICES = """
SELECT DISTINCT ON (product_id) product_id, price, currency, price_basis, price_unit_quantity, tax_mode
FROM latest_price_observations
WHERE product_id = ANY(:ids)
ORDER BY product_id, min_quantity NULLS FIRST, quantity NULLS FIRST, id
"""


def _sales_unit_price(row: Any) -> bool:
    return row["price_basis"] == "unit" and row["price_unit_quantity"] in (None, Decimal(1))


def comparable(competitor: Any, antalis: Any) -> bool:
    taxes = {competitor["tax_mode"], antalis["tax_mode"]} - {"unknown"}
    return (
        competitor["currency"] == antalis["currency"]
        and _sales_unit_price(competitor)
        and _sales_unit_price(antalis)
        and len(taxes) <= 1
        and antalis["price"] > 0
    )


def price_position(session: Session, reference_key: str) -> list[schemas.PricePosition]:
    Antalis = aliased(Product, name="antalis")
    pairs = session.execute(
        select(ProductMatch.competitor_id, ProductMatch.product_id, ProductMatch.antalis_product_id)
        .join(Product, Product.id == ProductMatch.product_id)
        .join(Antalis, Antalis.id == ProductMatch.antalis_product_id)
        .where(
            ProductMatch.status.in_(("auto_matched", "validated")),
            Product.status == "active",
            Antalis.status == "active",
        )
    ).all()
    ids = list({pid for _, *products in pairs for pid in products})
    prices = {row["product_id"]: row for row in session.execute(text(LATEST_FROM_PRICES), {"ids": ids}).mappings()}
    gaps: dict[int, list[float]] = {}
    for competitor_id, product_id, antalis_id in pairs:
        mine, ref = prices.get(product_id), prices.get(antalis_id)
        if mine is not None and ref is not None and comparable(mine, ref):
            gaps.setdefault(competitor_id, []).append(float((mine["price"] - ref["price"]) / ref["price"] * 100))
    result = []
    for competitor in session.scalars(select(Competitor).order_by(Competitor.name)):
        if competitor.key == reference_key:
            continue
        values = gaps.get(competitor.id, [])
        result.append(
            schemas.PricePosition(
                competitor_key=competitor.key,
                pairs=len(values),
                cheaper=sum(g <= -SAME_PRICE_PERCENT for g in values),
                same=sum(abs(g) < SAME_PRICE_PERCENT for g in values),
                pricier=sum(g >= SAME_PRICE_PERCENT for g in values),
                median_gap_percent=round(statistics.median(values), 2) if values else None,
            )
        )
    return result
