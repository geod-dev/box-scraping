"""Request/response models. Decimal values (prices, quantities) are serialized as strings
to stay exact; timestamps are ISO 8601 in UTC."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

T = TypeVar("T")

JobTask = Literal["catalog_sync", "price_refresh", "matching"]
JobStatus = Literal["queued", "running", "paused", "succeeded", "partial", "failed", "cancelled", "interrupted"]
WorkerState = Literal["idle", "queued", "running", "pausing", "paused", "resuming", "stopping"]
LifecycleStatus = Literal["active", "missing"]
CategoryRole = Literal["main", "extra"]
ROLE_DESCRIPTION = (
    "main: its subtree owns products (each product belongs to exactly one main category). extra: its "
    "subtree owns no product but lists, on the site, products filed under other categories (promotions, "
    "pallet sales...); an extra category can sit below a main one"
)
NotificationLevel = Literal["info", "success", "warning", "error"]
MatchStatus = Literal["auto_matched", "needs_review", "no_match", "validated", "rejected", "deprecated"]
MatchingMode = Literal["incremental", "deprecated", "all"]
MatchEventType = Literal["computed", "recomputed", "validated", "rejected", "deprecated", "reset"]
DeprecationReason = Literal[
    "product_changed",
    "antalis_product_changed",
    "antalis_product_missing",
    "antalis_product_deleted",
    "reset_requested",
]
TaskRefusal = Literal[
    "worker_busy",
    "catalog_required",
    "competitor_disabled",
    "matching_reference",
    "reference_missing",
    "matching_not_configured",
    "reference_catalog_required",
]
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
    retry_failed_run_id: int | None = Field(
        None,
        description="only the pages that failed in that catalog run: its failed product pages and failed listing "
        "pages with their sub-pages (no full crawl, never a full sync)",
    )
    category_id: int | None = Field(
        None,
        description="only this stored category and its whole subtree (sub-categories, products), even when it is "
        "empty (e.g. just unbanned); not combinable with root_urls / retry_failed_run_id. Never a full sync",
    )


class PriceRefreshOptionsIn(BaseModel):
    """Options of a price refresh. Leave everything empty to refresh every active product."""

    model_config = ConfigDict(extra="forbid")

    category: str | None = Field(None, description="category id, external id or URL (whole subtree)")
    references: list[str] = Field(default_factory=list, description="only these catalog references")
    retry_failed_run_id: int | None = Field(None, description="only the products that failed in that run")
    skip_fresh_hours: float | None = Field(None, ge=0, description="skip products priced less than N hours ago")
    include_missing: bool = Field(False, description="also refresh products flagged missing")
    update_details: bool = Field(
        True,
        description="also re-parse description/characteristics/images from the same product page (no extra "
        "request); false = prices only",
    )
    limit: int | None = Field(None, ge=1)


class CatalogSyncJobIn(BaseModel):
    task: Literal["catalog_sync"]
    options: CatalogSyncOptionsIn = Field(default_factory=CatalogSyncOptionsIn)


class PriceRefreshJobIn(BaseModel):
    task: Literal["price_refresh"]
    options: PriceRefreshOptionsIn = Field(default_factory=PriceRefreshOptionsIn)


class MatchingOptionsIn(BaseModel):
    """Options of a matching run against the reference catalog (Antalis)."""

    model_config = ConfigDict(extra="forbid")

    mode: MatchingMode = Field(
        "incremental",
        description="incremental: products never matched, deprecated matches and engine results computed with "
        "another configuration or reference catalog state (the normal case); deprecated: only the deprecated "
        "matches; all: every engine result. Human decisions (validated/rejected) are never recomputed unless "
        "deprecated.",
    )
    reembed: bool = Field(
        False,
        description="false (default): every text already embedded (by any product of any competitor) reuses its "
        "cached vector, only new texts are sent to Voyage AI, whatever the mode. true: embed every text of the run "
        "again and replace the cached vectors (paid; normally never needed).",
    )
    category_id: int | None = Field(
        None, description="only the products of this category of the competitor and of its sub-categories"
    )


class MatchingJobIn(BaseModel):
    task: Literal["matching"]
    options: MatchingOptionsIn = Field(default_factory=MatchingOptionsIn)


JobIn = Annotated[CatalogSyncJobIn | PriceRefreshJobIn | MatchingJobIn, Field(discriminator="task")]


class EstimateOut(BaseModel):
    task: JobTask
    listing_pages: int | None = None
    products: int | None = Field(None, description="product pages to fetch; matching: products to score")
    embeddings: int | None = Field(None, description="matching: product texts to embed (the others are cached)")
    requests: int | None = Field(None, description="HTTP requests (pages; matching: Voyage AI requests)")
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
    resumed_from_job_id: int | None = Field(None, description="the job this one restarts (manual or automatic)")
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
    reason: TaskRefusal | None = None


class WorkerOut(BaseModel):
    competitor_key: str
    state: WorkerState
    job: JobOut | None = Field(None, description="the active (queued/running/paused) job")
    last_job: JobOut | None = Field(None, description="the most recent finished job")
    tasks: dict[str, TaskAvailability]


# --------------------------------------------------------------------------- competitors


class CompetitorStats(BaseModel):
    categories: int
    banned_categories: int = Field(0, description="categories banned (deleted and skipped by the crawls)")
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
    role: CategoryRole = Field(description=ROLE_DESCRIPTION)
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
    listed_product_count: int = Field(
        description="active products shown in this category and its descendants: the ones they own and the ones "
        "they list from other categories (what GET /products?category_id= returns); the count of an extra category"
    )


class CategoryTreeNode(BaseModel):
    id: int
    name: str
    kind: str | None
    status: LifecycleStatus
    external_id: str | None
    role: CategoryRole = Field(description=ROLE_DESCRIPTION)
    product_count: int = Field(description="active products directly in this category")
    subtree_product_count: int = Field(description="active products owned by this category and its descendants")
    listed_product_count: int = Field(
        description="active products shown in this category and its descendants, the ones listed from other "
        "categories included (what GET /products?category_id= returns)"
    )
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


class BanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(None, description="why it is banned (shown in the banned list)")
    banned_by: str | None = Field(None, description="who banned it")


class BannedCategoryOut(Orm):
    id: int = Field(description="ban id (used to unban)")
    competitor_id: int
    competitor_key: str
    category_id: int = Field(description="id the category had before the ban (it is a new id after unban)")
    name: str
    slug: str | None
    url: str
    external_id: str | None
    kind: str | None
    depth: int | None
    breadcrumb: list[Any] | None
    details: dict[str, Any]
    parent_url: str | None
    parent_category_id: int | None = Field(None, description="current id of its parent category, if it exists")
    first_seen_at: datetime | None
    reason: str | None
    banned_by: str | None
    banned_at: datetime
    deleted: dict[str, int] = Field(
        description="what the ban deleted: categories, products, observations, matches, matches_deprecated "
        "(matches of other competitors that pointed to a deleted reference product)"
    )


class UnbanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scrape: bool = Field(
        False,
        description="also queue a catalog sync of the recreated category (refused as a whole, nothing unbanned, "
        "if the competitor's worker is busy)",
    )
    unbanned_by: str | None = None


class UnbanOut(BaseModel):
    category: CategoryOut = Field(description="the recreated category (empty, new id)")
    job: JobOut | None = Field(None, description="the catalog sync queued when scrape=true")


class CategorySyncIn(BaseModel):
    """Catalog sync of one category: its sub-categories and products, details and prices."""

    model_config = ConfigDict(extra="forbid")

    max_products: int | None = Field(None, ge=0, description="only sync the first N products")
    skip_products: bool = Field(False, description="only crawl sub-categories and product URLs")
    resume_within_hours: float | None = Field(
        48, ge=0, description="products synced less than N hours ago are not fetched again (0 = refetch all)"
    )


class CategoryMatchIn(BaseModel):
    """Matching of the products of one category (and its sub-categories)."""

    model_config = ConfigDict(extra="forbid")

    mode: MatchingMode = Field("incremental", description="same as POST /matching/competitors/{key}/run")
    reembed: bool = False


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


class PriceTrend(BaseModel):
    """Last change of the "from" price (first tier) between two captures with the same currency,
    unit basis and tax mode."""

    previous_price: Decimal = Field(description="from price before the change")
    change_percent: float = Field(description="(new - previous) / previous * 100, rounded to 2 decimals")
    changed_at: datetime = Field(description="capture where the new price was first observed")


# --------------------------------------------------------------------------- products


class ProductOut(Orm):
    id: int
    competitor_id: int
    category_id: int | None
    identity_key: str
    catalog_reference: str | None
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
    price_trend: PriceTrend | None = Field(description="last change of the from price; null if it never changed")


class MatchBriefOut(BaseModel):
    """Matching state of a competitor product (null for reference products and unmatched ones)."""

    match_id: int
    status: MatchStatus
    antalis_product_id: int | None
    proposed_antalis_product_id: int | None
    score: float | None


class ProductDetailOut(ProductListItem):
    long_description: str | None
    attributes: list[Any] | None
    extra: dict[str, Any]
    last_seen_run_id: int | None
    category_path: list[CategoryRefOut]
    listed_in: list[CategoryRefOut] = Field(
        description="other categories that list this product on the site (extra categories: promotions, pallet "
        "sales...), directly or through its family; by name"
    )
    latest_prices: list[ObservationOut] = Field(description="all tiers of the latest capture")
    match_count: int = Field(
        description="associations (auto_matched or validated) involving this product: competitor products "
        "associated with it for a reference product, 0 or 1 for a competitor product"
    )
    matching: MatchBriefOut | None = None


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competitor: str = Field(description="competitor key")
    url: str
    catalog_reference: str | None = Field(None, description="identity: ref:<reference>, else url:<url>")
    category_id: int | None = None
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


# --------------------------------------------------------------------------- matching

SCORE_DESCRIPTION = "similarity score on a 0-100 scale (NOT a probability of equivalence)"


class ProductBriefOut(BaseModel):
    id: int
    competitor_id: int
    competitor_key: str
    catalog_reference: str | None
    title: str | None
    url: str
    brand: str | None
    category: str | None
    status: LifecycleStatus
    normalized_attributes: dict[str, Any] | None = None
    image_url: str | None = Field(None, description="first product image")


class ReasonOut(BaseModel):
    code: str = Field(
        description="score_above_auto, clear_gap, single_candidate, close_runner_up, ambiguous_gap, review_band, "
        "score_below_review, no_compatible_candidate, feature_conflict, dimension_mismatch, "
        "dimensions_not_comparable, previously_validated, previous_association_changed"
    )
    message: str
    warning: bool = Field(
        False,
        description="true for a reason that did not block the status but deserves a look (close_runner_up: "
        "associated automatically although a second candidate is close)",
    )


class MatchListItem(BaseModel):
    id: int
    competitor_id: int
    competitor_key: str
    status: MatchStatus = Field(
        description="auto_matched / validated: associated with antalis_product (engine / human); needs_review: "
        "proposal to examine; no_match: no acceptable candidate; rejected: a human confirmed there is no "
        "equivalent; deprecated: the data changed since, rerun needed"
    )
    product: ProductBriefOut = Field(description="the competitor product")
    antalis_product: ProductBriefOut | None = Field(description="the associated reference product")
    proposed_antalis_product: ProductBriefOut | None = Field(description="the engine's best candidate")
    score: float | None = Field(description=SCORE_DESCRIPTION)
    runner_up_score: float | None = Field(description="score of the second candidate")
    score_gap: float | None = Field(description="lead of the best candidate over the second one")
    dimensions_compared: bool
    decision_reasons: list[ReasonOut]
    decided_by: str | None
    decided_at: datetime | None
    decision_comment: str | None
    manual_choice: bool = Field(description="validated with another product than the engine's proposal")
    deprecated_at: datetime | None
    deprecated_reason: DeprecationReason | None
    previous_status: MatchStatus | None = Field(description="status before deprecation")
    computed_at: datetime | None
    job_id: int | None
    outdated_config: bool = Field(description="engine result computed with another matching configuration")
    revision: int
    created_at: datetime
    updated_at: datetime


class MeasureOut(BaseModel):
    key: str
    label: str
    unit: str
    competitor: list[float] | None
    reference: list[float] | None
    competitor_source: str | None = Field(None, description="attributes or title")
    reference_source: str | None = None
    relative_difference: float | None = None
    score: float | None = Field(None, description="0..1; null when the measure is missing on one side")


class SignalOut(BaseModel):
    cosine: float | None = Field(description="raw cosine similarity")
    score: float | None = Field(description="normalized 0..1 sub-score")


class LexicalOut(SignalOut):
    shared_terms: list[str] = Field(default_factory=list)


class DimensionsOut(BaseModel):
    comparable: bool
    score: float | None
    measures: list[MeasureOut]


class FeatureOut(BaseModel):
    key: str = Field(description="product_type or a characteristic of config/matching.yaml")
    competitor: Any
    reference: Any
    status: Literal["match", "conflict", "unknown"]
    policy: Literal["exclude", "review"] | None = None


class DifferenceOut(BaseModel):
    kind: Literal["dimension", "feature"]
    field: str
    label: str | None = None
    competitor: Any
    reference: Any
    unit: str | None = None
    relative_difference: float | None = None


class FlagOut(BaseModel):
    code: str
    message: str
    field: str | None = None


class CandidateOut(BaseModel):
    rank: int | None = Field(description="1 = best; null for an excluded candidate")
    excluded: bool
    antalis_product: ProductBriefOut
    score: float = Field(description=SCORE_DESCRIPTION)
    semantic_score: float | None
    lexical_score: float | None
    dimension_score: float | None = Field(description="null = dimensions not comparable")
    is_proposed: bool
    is_associated: bool
    rejected_by_human: bool
    weights: dict[str, float] = Field(description="weights applied to the sub-scores")
    semantic: SignalOut
    lexical: LexicalOut
    dimensions: DimensionsOut
    features: list[FeatureOut]
    differences: list[DifferenceOut]
    exclusions: list[FlagOut] = Field(description="certain incompatibilities: the candidate cannot be associated")
    review_flags: list[FlagOut] = Field(description="conflicts that forbid an automatic association")
    retrieved_by: list[str] = Field(description="retrieval signals that found it: semantic, lexical, dimensions")


class MatchEventOut(BaseModel):
    id: int
    match_id: int
    product_id: int
    competitor_id: int
    event: MatchEventType
    from_status: MatchStatus | None
    to_status: MatchStatus | None
    from_antalis_product_id: int | None
    to_antalis_product_id: int | None
    score: float | None
    actor: str
    reason: str | None
    job_id: int | None
    data: dict[str, Any] = Field(description="snapshot of the previous state")
    created_at: datetime
    product_title: str | None = None


class DimensionlessProductOut(BaseModel):
    product_id: int
    catalog_reference: str | None
    title: str | None
    category: str | None
    url: str | None
    product_type: str | None = Field(description="type read in the title, else the category name")
    title_has_numbers: bool = Field(description="the title has numbers but no measure was read: likely a parsing gap")
    normalized_attributes: dict[str, Any]


class DimensionlessPage(Page[DimensionlessProductOut]):
    by_product_type: dict[str, int] = Field(
        description="all products without dimensions of the competitor (before filters), by product type "
        '("unknown" = none detected)'
    )


class ProductAnalysisOut(BaseModel):
    """What the engine read of the competitor product."""

    title: str
    description_used: str | None
    category: str | None
    dimensions: dict[str, Any]
    product_type: str | None
    product_type_source: str | None
    features: dict[str, list[str]]


class MatchDetailOut(MatchListItem):
    analysis: ProductAnalysisOut | None = Field(description="null when the product can no longer be read")
    candidates: list[CandidateOut] = Field(description="latest computation: ranked candidates, then excluded ones")
    rejected_antalis_products: list[ProductBriefOut]
    history: list[MatchEventOut]


class CompareOut(BaseModel):
    product: ProductBriefOut
    analysis: ProductAnalysisOut
    candidate: CandidateOut
    semantic_available: bool = Field(description="false: an embedding is missing, the score uses the other criteria")


class ValidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    antalis_product_id: int | None = Field(
        None, description="reference product to associate; default: the engine's proposal"
    )
    decided_by: str | None = Field(None, description="name of the reviewer (history)")
    comment: str | None = None


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decided_by: str | None = None
    comment: str | None = None


class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decided_by: str | None = None
    clear_rejections: bool = Field(False, description="also forget the products rejected by reviewers")


class MatchingAvailability(BaseModel):
    allowed: bool
    reason: TaskRefusal | None = None
    message: str | None = None


class MatchingSummaryOut(BaseModel):
    competitor_id: int
    competitor_key: str
    competitor_name: str
    availability: MatchingAvailability = Field(description="whether a matching run can start now")
    counts: dict[str, int] = Field(description="matches per status")
    eligible_products: int = Field(description="active products with parsed details")
    never_matched: int
    needs_review: int
    deprecated: int
    outdated_by_config: int | None
    embedding_tokens: int = Field(description="Voyage tokens spent on this competitor's products")
    last_computed_at: datetime | None
    job: JobOut | None = Field(None, description="active matching job")
    last_job: JobOut | None = Field(None, description="most recent finished matching job")


class MatchingConfigOut(BaseModel):
    reference_competitor: str
    configured: bool = Field(description="false when VOYAGE_API_KEY is not set")
    engine_version: str
    thresholds: dict[str, float]
    weights: dict[str, dict[str, float]]
    embedding: dict[str, Any]
    config: dict[str, Any] = Field(description="the whole config/matching.yaml")


class RerunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competitor: str | None = Field(None, description="competitor key; default: every competitor")


class RerunRefusal(BaseModel):
    competitor_key: str
    code: str
    message: str


class RerunOut(BaseModel):
    jobs: list[JobOut]
    refused: list[RerunRefusal]
    nothing_to_do: list[str] = Field(description="competitors without deprecated matches")


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


class DailyStats(BaseModel):
    date: str = Field(description="YYYY-MM-DD in the requested time zone")
    observations: int = Field(description="price observations (tiers) captured")
    products_created: int = Field(description="products first seen")
    products_marked_missing: int = Field(description="products flagged missing by the catalog syncs finished that day")
    price_changes: int = Field(description="captures whose from price differs from the previous comparable capture")
    credits_used: int = Field(description="Scrape.do credits of the runs started that day")
    jobs_succeeded: int
    jobs_failed: int
    matches_auto: int = Field(description="engine results that became auto_matched")
    matches_validated: int
    matches_rejected: int


class JobStatusTotals(BaseModel):
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    cancelled: int = 0
    interrupted: int = 0


class StatsTotals(BaseModel):
    observations: int
    products_created: int
    credits_used: int
    price_increases: int
    price_decreases: int
    jobs: JobStatusTotals = Field(description="jobs finished over the period, by final status")


class CreditsOut(BaseModel):
    monthly_limit: int | None = Field(description="Scrape.do monthly request quota")
    monthly_remaining: int | None
    renews_at: datetime | None = Field(description="not given by Scrape.do: null")
    error: str | None = Field(None, description="why the figures are null (no key, Scrape.do unavailable)")


class PricePosition(BaseModel):
    competitor_key: str
    pairs: int = Field(description="associated pairs with a comparable latest from price on both sides")
    cheaper: int = Field(description="competitor price lower by 1 % or more")
    same: int = Field(description="|gap| < 1 %")
    pricier: int = Field(description="competitor price higher by 1 % or more")
    median_gap_percent: float | None = Field(description="median of (competitor - antalis) / antalis * 100")


class StatsOverview(BaseModel):
    days: int
    daily: list[DailyStats] = Field(description="one row per day, oldest first, zeros included")
    totals: StatsTotals
    credits: CreditsOut = Field(description="Scrape.do quota, cached about 60 s")
    price_position: list[PricePosition] = Field(description="per non-reference competitor")


# --------------------------------------------------------------------------- events (SSE)


class ConsoleCompetitor(BaseModel):
    key: str
    name: str
    enabled: bool
    worker_state: WorkerState
    job_id: int | None = Field(description="active job")


class MatchingTotals(BaseModel):
    needs_review: int
    deprecated: int


class ConsoleSummary(BaseModel):
    """Payload of the ``console.summary`` event."""

    jobs: list[JobOut] = Field(description="active jobs (queued/running/paused), newest first")
    competitors: list[ConsoleCompetitor]
    matching: MatchingTotals = Field(description="totals across the non-reference competitors")
    notifications: UnreadCount
