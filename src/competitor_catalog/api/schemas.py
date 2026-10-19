"""Request/response models. Decimal values (prices, quantities) are serialized as strings
to stay exact; timestamps are ISO 8601 in UTC."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

T = TypeVar("T")

JobTask = Literal["catalog_sync", "price_refresh"]
JobStatus = Literal["queued", "running", "paused", "succeeded", "partial", "failed", "cancelled", "interrupted"]
WorkerState = Literal["idle", "queued", "running", "pausing", "paused", "resuming", "stopping"]
LifecycleStatus = Literal["active", "missing"]
NotificationLevel = Literal["info", "success", "warning", "error"]
MatchStatus = Literal["proposed", "validated", "rejected"]
RunOperation = Literal["catalog_discovery", "price_refresh"]
RunStatus = Literal["running", "succeeded", "partial", "failed", "aborted"]
ItemType = Literal["listing_page", "product"]
ItemStatus = Literal["succeeded", "failed", "not_found", "no_price", "skipped"]


class Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):  # noqa: UP046 - pydantic generic model
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# --------------------------------------------------------------------------- jobs / workers


class CatalogSyncOptionsIn(BaseModel):
    """Options of a catalog sync. Leave everything empty for a full sync (the normal case)."""

    model_config = ConfigDict(extra="forbid")

    root_urls: list[str] | None = Field(None, description="crawl only from these URLs (partial scope)")
    max_listing_pages: int | None = Field(None, ge=1, description="stop the crawl after N listing pages (partial)")
    max_products: int | None = Field(None, ge=0, description="only sync the first N products (partial)")
    skip_products: bool = Field(False, description="only crawl categories and product URLs (partial)")
    resume_within_hours: float | None = Field(
        48, ge=0, description="products synced less than N hours ago are not fetched again (0 = refetch all)"
    )
    allow_mass_missing: bool = Field(False, description="bypass the 'too many products missing' safety threshold")
    max_missing_ratio: float = Field(0.2, ge=0, le=1)


class PriceRefreshOptionsIn(BaseModel):
    """Options of a price refresh. Leave everything empty to refresh every active product."""

    model_config = ConfigDict(extra="forbid")

    category: str | None = Field(None, description="category id, external id or URL (whole subtree)")
    references: list[str] = Field(default_factory=list, description="only these catalog references")
    retry_failed_run_id: int | None = Field(None, description="only the products that failed in that run")
    skip_fresh_hours: float | None = Field(None, ge=0, description="skip products priced less than N hours ago")
    include_missing: bool = Field(False, description="also refresh products flagged missing")
    update_details: bool = Field(False, description="also update description/characteristics/images")
    limit: int | None = Field(None, ge=1)


class CatalogSyncJobIn(BaseModel):
    task: Literal["catalog_sync"]
    options: CatalogSyncOptionsIn = Field(default_factory=CatalogSyncOptionsIn)


class PriceRefreshJobIn(BaseModel):
    task: Literal["price_refresh"]
    options: PriceRefreshOptionsIn = Field(default_factory=PriceRefreshOptionsIn)


JobIn = Annotated[CatalogSyncJobIn | PriceRefreshJobIn, Field(discriminator="task")]


class EstimateOut(BaseModel):
    task: JobTask
    listing_pages: int | None = None
    products: int | None = None
    requests: int | None = Field(None, description="HTTP requests (pages) the task will make")
    seconds_per_request: float
    seconds: float | None = Field(None, description="estimated duration")
    credits: int | None = Field(None, description="estimated Scrape.do credits")
    basis: list[str] = Field(default_factory=list, description="where each figure comes from")


class StepOut(BaseModel):
    name: str
    label: str
    status: Literal["pending", "running", "done"]
    done: int = 0
    goal: int | None = None
    pending: int | None = None


class ProgressOut(BaseModel):
    percent: float | None = Field(None, description="0-100; null while the total is unknown")
    requests_done: int = 0
    requests_total: int | None = None
    requests_total_is_estimate: bool = True
    seconds_per_request: float | None = None
    seconds_per_request_measured: bool = False
    elapsed_seconds: float | None = None
    paused_seconds: float = 0
    eta_seconds: float | None = None
    estimated_finish_at: datetime | None = None
    errors: int = 0
    steps: list[StepOut] = Field(default_factory=list)


class JobOut(Orm):
    id: int
    competitor_id: int
    competitor_key: str
    task: JobTask
    status: JobStatus
    options: dict[str, Any]
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    cancel_requested_at: datetime | None
    pause_requested_at: datetime | None = Field(description="set while a pause is requested/active")
    paused_at: datetime | None = Field(description="when the worker actually paused")
    scrape_run_id: int | None
    step: str | None
    step_label: str | None
    step_done: int
    step_goal: int | None
    progress: ProgressOut
    counters: dict[str, Any] = Field(description="run counters (pages_fetched, products_seen, credits_used...)")
    estimate: EstimateOut | None
    error: str | None
    duration_seconds: float | None


class TaskAvailability(BaseModel):
    allowed: bool
    reason: Literal["worker_busy", "catalog_required", "competitor_disabled"] | None = None


class WorkerOut(BaseModel):
    competitor_key: str
    state: WorkerState
    job: JobOut | None = Field(None, description="the active (queued/running/paused) job")
    last_job: JobOut | None = Field(None, description="the most recent finished job")
    tasks: dict[str, TaskAvailability]


# --------------------------------------------------------------------------- competitors


class CompetitorStats(BaseModel):
    categories: int
    products_active: int
    products_missing: int
    observations: int
    last_price_observed_at: datetime | None


class CompetitorOut(Orm):
    id: int
    key: str
    name: str
    base_url: str
    adapter: str
    enabled: bool
    last_full_catalog_at: datetime | None
    last_full_catalog_run_id: int | None
    last_full_catalog_status: str | None
    last_price_refresh_at: datetime | None
    created_at: datetime
    updated_at: datetime
    stats: CompetitorStats
    worker: WorkerOut


class CompetitorDetailOut(CompetitorOut):
    config: dict[str, Any]
    estimates: dict[str, EstimateOut]


# --------------------------------------------------------------------------- categories


class CategoryOut(Orm):
    id: int
    competitor_id: int
    parent_id: int | None
    name: str
    slug: str | None
    url: str
    external_id: str | None
    kind: str | None
    depth: int | None
    breadcrumb: list[Any] | None
    details: dict[str, Any]
    status: LifecycleStatus
    first_seen_at: datetime
    last_seen_at: datetime
    last_seen_run_id: int | None
    missing_since: datetime | None
    created_at: datetime
    updated_at: datetime


class CategoryRefOut(Orm):
    id: int
    name: str
    url: str
    external_id: str | None


class CategoryDetailOut(CategoryOut):
    path: list[CategoryRefOut] = Field(description="ancestors from the root, the category itself last")
    children_count: int
    product_count: int = Field(description="active products directly in this category")
    subtree_product_count: int = Field(description="active products in this category and its descendants")


class CategoryTreeNode(BaseModel):
    id: int
    name: str
    kind: str | None
    status: LifecycleStatus
    external_id: str | None
    product_count: int
    subtree_product_count: int
    children: list[CategoryTreeNode] = Field(default_factory=list)


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competitor: str = Field(description="competitor key")
    name: str
    url: str
    parent_id: int | None = None
    external_id: str | None = None
    slug: str | None = None
    kind: str | None = None
    depth: int | None = None
    breadcrumb: list[Any] | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    status: LifecycleStatus = "active"


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    url: str | None = None
    parent_id: int | None = None
    external_id: str | None = None
    slug: str | None = None
    kind: str | None = None
    depth: int | None = None
    breadcrumb: list[Any] | None = None
    details: dict[str, Any] | None = None
    status: LifecycleStatus | None = None


# --------------------------------------------------------------------------- prices


class ObservationOut(Orm):
    id: int
    product_id: int
    scrape_run_id: int | None
    capture_id: uuid.UUID
    observed_at: datetime
    quantity: Decimal | None
    min_quantity: Decimal | None
    max_quantity: Decimal | None
    max_inclusive: bool
    quantity_unit: str | None
    price: Decimal
    currency: str
    price_basis: Literal["unit", "total", "unknown"]
    price_unit: str | None
    price_unit_quantity: Decimal | None
    tax_mode: Literal["excl_tax", "incl_tax", "unknown"]
    is_promotional: bool | None
    metadata: dict[str, Any] = Field(validation_alias=AliasChoices("metadata_", "metadata"))


class ObservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int
    price: Decimal = Field(ge=0)
    currency: str = Field("EUR", pattern="^[A-Z]{3}$")
    observed_at: datetime | None = Field(None, description="default: now")
    capture_id: uuid.UUID | None = Field(None, description="tiers of one capture share an id; default: new")
    scrape_run_id: int | None = None
    quantity: Decimal | None = None
    min_quantity: Decimal | None = None
    max_quantity: Decimal | None = None
    max_inclusive: bool = False
    quantity_unit: str | None = None
    price_basis: Literal["unit", "total", "unknown"] = "unit"
    price_unit: str | None = None
    price_unit_quantity: Decimal | None = None
    tax_mode: Literal["excl_tax", "incl_tax", "unknown"] = "excl_tax"
    is_promotional: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: Decimal | None = Field(None, ge=0)
    currency: str | None = Field(None, pattern="^[A-Z]{3}$")
    observed_at: datetime | None = None
    quantity: Decimal | None = None
    min_quantity: Decimal | None = None
    max_quantity: Decimal | None = None
    max_inclusive: bool | None = None
    quantity_unit: str | None = None
    price_basis: Literal["unit", "total", "unknown"] | None = None
    price_unit: str | None = None
    price_unit_quantity: Decimal | None = None
    tax_mode: Literal["excl_tax", "incl_tax", "unknown"] | None = None
    is_promotional: bool | None = None
    metadata: dict[str, Any] | None = None


class PriceSummary(BaseModel):
    """Latest capture of a product, summarized for lists."""

    observed_at: datetime
    currency: str
    from_price: Decimal = Field(description="price of the first tier (smallest quantity)")
    min_price: Decimal = Field(description="lowest price over the tiers (best degressive price)")
    tiers: int
    price_basis: str
    price_unit: str | None
    price_unit_quantity: Decimal | None
    quantity_unit: str | None
    tax_mode: str
    displayed: bool = Field(description="false when the site shows no price (value from structured data)")


# --------------------------------------------------------------------------- products


class ProductOut(Orm):
    id: int
    competitor_id: int
    category_id: int | None
    identity_key: str
    catalog_reference: str | None
    antalis_reference: str | None
    external_id: str | None
    url: str
    title: str | None
    brand: str | None
    status: LifecycleStatus
    first_seen_at: datetime
    last_seen_at: datetime
    missing_since: datetime | None
    details_updated_at: datetime | None
    last_price_observed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProductListItem(ProductOut):
    description: str | None
    normalized_attributes: dict[str, Any] | None
    images: list[Any] | None
    latest_price: PriceSummary | None


class ProductDetailOut(ProductListItem):
    long_description: str | None
    attributes: list[Any] | None
    extra: dict[str, Any]
    last_seen_run_id: int | None
    category_path: list[CategoryRefOut]
    latest_prices: list[ObservationOut] = Field(description="all tiers of the latest capture")
    match_count: int


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competitor: str = Field(description="competitor key")
    url: str
    catalog_reference: str | None = Field(None, description="identity: ref:<reference>, else url:<url>")
    category_id: int | None = None
    antalis_reference: str | None = None
    external_id: str | None = None
    title: str | None = None
    description: str | None = None
    long_description: str | None = None
    brand: str | None = None
    attributes: list[Any] | None = None
    normalized_attributes: dict[str, Any] | None = None
    images: list[Any] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    status: LifecycleStatus = "active"


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    catalog_reference: str | None = None
    category_id: int | None = None
    antalis_reference: str | None = None
    external_id: str | None = None
    title: str | None = None
    description: str | None = None
    long_description: str | None = None
    brand: str | None = None
    attributes: list[Any] | None = None
    normalized_attributes: dict[str, Any] | None = None
    images: list[Any] | None = None
    extra: dict[str, Any] | None = None
    status: LifecycleStatus | None = None


class FacetValue(BaseModel):
    value: Any
    count: int


class ProductFacets(BaseModel):
    total: int
    brands: list[FacetValue]
    statuses: list[FacetValue]
    attributes: dict[str, list[FacetValue]] = Field(description="normalized attribute -> most frequent values")


# --------------------------------------------------------------------------- runs


class RunOut(Orm):
    id: int
    competitor_id: int
    operation: RunOperation
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    heartbeat_at: datetime | None
    full_scope: bool
    params: dict[str, Any]
    pages_fetched: int
    pages_failed: int
    categories_seen: int
    products_seen: int
    products_created: int
    products_updated: int
    products_failed: int
    products_without_price: int
    observations_created: int
    categories_marked_missing: int
    products_marked_missing: int
    credits_used: int
    error: str | None


class RunDetailOut(RunOut):
    stats: dict[str, Any]
    item_counts: dict[str, dict[str, int]] = Field(description="item_type -> status -> count")


class RunItemOut(Orm):
    id: int
    scrape_run_id: int
    item_type: ItemType
    url: str
    product_id: int | None
    category_id: int | None
    status: ItemStatus
    error_type: str | None
    error_message: str | None
    http_status: int | None
    warnings: list[Any] | None
    created_at: datetime


# --------------------------------------------------------------------------- matches


class MatchOut(Orm):
    id: int
    product_a_id: int
    product_b_id: int
    match_type: str
    confidence: Decimal | None
    status: MatchStatus
    method: str | None
    evidence: dict[str, Any]
    validated_by: str | None
    validated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_a_id: int
    product_b_id: int
    match_type: str = "equivalent"
    confidence: Decimal | None = Field(None, ge=0, le=1)
    status: MatchStatus = "proposed"
    method: str | None = "manual"
    evidence: dict[str, Any] = Field(default_factory=dict)
    validated_by: str | None = None


class MatchUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_type: str | None = None
    confidence: Decimal | None = Field(None, ge=0, le=1)
    status: MatchStatus | None = None
    method: str | None = None
    evidence: dict[str, Any] | None = None
    validated_by: str | None = None


# --------------------------------------------------------------------------- notifications


class NotificationOut(Orm):
    id: int
    created_at: datetime
    level: NotificationLevel
    kind: str
    title: str
    message: str | None
    competitor_id: int | None
    job_id: int | None
    scrape_run_id: int | None
    data: dict[str, Any]
    read_at: datetime | None
    read: bool


class UnreadCount(BaseModel):
    unread: int
    by_level: dict[str, int]


class MarkedCount(BaseModel):
    updated: int


# --------------------------------------------------------------------------- system


class WorkerServiceStatus(BaseModel):
    online: bool
    instance: str | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    running_jobs: list[int] = Field(default_factory=list)
    auto_schedule: bool = False


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: bool
    worker: WorkerServiceStatus


class StatsOut(BaseModel):
    competitors: int
    categories: int
    products_active: int
    products_missing: int
    observations: int
    runs: int
    active_jobs: int
    unread_notifications: int
