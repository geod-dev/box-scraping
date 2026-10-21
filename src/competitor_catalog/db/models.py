"""PostgreSQL schema (SQLAlchemy 2.0 declarative). Migrations live in ``migrations/``.

Design notes are in README.md ("Database model"); the key rules are:

* Categories/products are competitor-specific. Uniqueness: a category is identified by a
  real site id when the site exposes one (``external_id``), otherwise by canonical URL;
  a product by ``identity_key`` (``ref:<catalog_reference>`` or, as a documented
  fallback, ``url:<canonical_url>``).
* ``scrape_observations`` is append-only price history: no uniqueness on
  (product, quantity) so the same tier can be observed many times.
* ``scrape_run_items`` records the outcome per page/product of each run, which is how a
  failed scrape is told apart from a page that has no price.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

LIFECYCLE_STATUSES = ("active", "missing")
RUN_OPERATIONS = ("catalog_discovery", "price_refresh")
RUN_STATUSES = ("running", "succeeded", "partial", "failed", "aborted")
ITEM_TYPES = ("listing_page", "product")
ITEM_STATUSES = ("succeeded", "failed", "not_found", "no_price", "skipped")
PRICE_BASES = ("unit", "total", "unknown")
TAX_MODES = ("excl_tax", "incl_tax", "unknown")
# auto_matched / validated: associated with antalis_product_id (by the engine / by a human);
# needs_review: proposal waiting for a human; no_match: no acceptable candidate (engine);
# rejected: a human confirmed there is no equivalent; deprecated: the data changed since.
MATCH_STATUSES = ("auto_matched", "needs_review", "no_match", "validated", "rejected", "deprecated")
MATCH_HUMAN_STATUSES = ("validated", "rejected")
MATCH_ENGINE_STATUSES = ("auto_matched", "needs_review", "no_match")
MATCH_EVENTS = ("computed", "recomputed", "validated", "rejected", "deprecated", "reset")
JOB_TASKS = ("catalog_sync", "price_refresh", "matching")
JOB_STATUSES = ("queued", "running", "paused", "succeeded", "partial", "failed", "cancelled", "interrupted")
JOB_ACTIVE_STATUSES = ("queued", "running", "paused")
NOTIFICATION_LEVELS = ("info", "success", "warning", "error")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# Nullable JSONB columns: Python None must be SQL NULL ("not observed"), not JSON 'null'.
NullableJSONB = JSONB(none_as_null=True)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # ``__table_comment__`` -> PostgreSQL table comment (kept identical to the migrations).
        comment = cls.__dict__.get("__table_comment__")
        if comment:
            args = cls.__dict__.get("__table_args__", ())
            cls.__table_args__ = (
                (*args, {"comment": comment}) if isinstance(args, tuple) else {**args, "comment": comment}
            )
        super().__init_subclass__(**kwargs)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Competitor(TimestampMixin, Base):
    __tablename__ = "competitors"
    __table_comment__ = "One row per competitor/supplier; behaviour comes from config/competitors/<key>.yaml"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)  # config key, e.g. "antalis_fr"
    name: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(Text)  # import path of the adapter class
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Last full-scope catalog sync that crawled the whole catalog (status succeeded or partial).
    last_full_catalog_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_catalog_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scrape_runs.id", ondelete="SET NULL", use_alter=True, name="fk_competitors_last_full_catalog_run")
    )
    last_full_catalog_status: Mapped[str | None] = mapped_column(Text)
    last_price_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Category(TimestampMixin, Base):
    __tablename__ = "categories"
    __table_comment__ = "Competitor-specific category hierarchy discovered through site navigation"
    __table_args__ = (
        UniqueConstraint("competitor_id", "url", name="uq_categories_competitor_url"),
        Index(
            "uq_categories_competitor_external_id",
            "competitor_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("ix_categories_parent_id", "parent_id"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_categories_not_own_parent"),
        CheckConstraint(_in("status", LIFECYCLE_STATUSES), name="ck_categories_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="RESTRICT"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(Text)  # last URL path segment, informational only
    url: Mapped[str] = mapped_column(Text)  # canonical URL
    external_id: Mapped[str | None] = mapped_column(Text)  # only a real id exposed by the site
    kind: Mapped[str | None] = mapped_column(Text)  # competitor-specific node type
    depth: Mapped[int | None] = mapped_column(Integer)  # 0 = crawl root
    breadcrumb: Mapped[list[Any] | None] = mapped_column(NullableJSONB)  # navigation path as observed
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id", ondelete="SET NULL"))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    parent: Mapped[Category | None] = relationship(remote_side="Category.id")


class BannedCategory(Base):
    """A category deleted (with its sub-categories, products, prices and matches) and excluded
    from scraping. Only its own metadata is kept, enough to recreate it (empty) on unban."""

    __tablename__ = "banned_categories"
    __table_comment__ = "Categories excluded from scraping; their own metadata is kept to recreate them on unban"
    __table_args__ = (
        UniqueConstraint("competitor_id", "url", name="uq_banned_categories_competitor_url"),
        Index("ix_banned_categories_competitor_external_id", "competitor_id", "external_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"))
    category_id: Mapped[int] = mapped_column(
        BigInteger, comment="id of the deleted category (plain id: the row no longer exists)"
    )
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, comment="canonical URL: crawls skip it and every page below it")
    external_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(Text)
    depth: Mapped[int | None] = mapped_column(Integer)
    breadcrumb: Mapped[list[Any] | None] = mapped_column(NullableJSONB)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    parent_url: Mapped[str | None] = mapped_column(Text, comment="URL of the parent, to link it again on unban")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    banned_by: Mapped[str | None] = mapped_column(Text)
    banned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), comment="what the ban deleted (counts per kind)"
    )


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_comment__ = "One product/reference in one competitor catalog (never shared across competitors)"
    __table_args__ = (
        UniqueConstraint("competitor_id", "identity_key", name="uq_products_competitor_identity"),
        Index("ix_products_competitor_reference", "competitor_id", "catalog_reference"),
        Index("ix_products_antalis_reference", "antalis_reference"),
        Index("ix_products_category_id", "category_id"),
        Index("ix_products_competitor_url", "competitor_id", "url"),
        Index(
            "ix_products_normalized_attributes",
            "normalized_attributes",
            postgresql_using="gin",
            postgresql_ops={"normalized_attributes": "jsonb_path_ops"},
        ),
        CheckConstraint(_in("status", LIFECYCLE_STATUSES), name="ck_products_status"),
        CheckConstraint("identity_key ~ '^(ref|url):.+'", name="ck_products_identity_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="RESTRICT"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    identity_key: Mapped[str] = mapped_column(
        Text, comment="ref:<catalog_reference>, or url:<canonical url> when the site exposes no stable reference"
    )
    catalog_reference: Mapped[str | None] = mapped_column(Text)
    # DEPRECATED, never read nor written: kept until a later migration drops it, so that the
    # previous release (still running during a deploy) can write it. Do not use.
    antalis_reference: Mapped[str | None] = mapped_column(
        Text,
        deferred=True,
        comment="Corresponding Antalis reference (own reference for Antalis, cross-reference for others)",
    )
    external_id: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)  # exact product detail URL (canonical)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    long_description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[list[Any] | None] = mapped_column(NullableJSONB)
    normalized_attributes: Mapped[dict[str, Any] | None] = mapped_column(NullableJSONB)
    images: Mapped[list[Any] | None] = mapped_column(NullableJSONB)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id", ondelete="SET NULL"))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_price_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    category: Mapped[Category | None] = relationship()


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"
    __table_comment__ = "One catalog discovery or price refresh execution"
    __table_args__ = (
        CheckConstraint(_in("operation", RUN_OPERATIONS), name="ck_scrape_runs_operation"),
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_scrape_runs_status"),
        Index("ix_scrape_runs_competitor_started", "competitor_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="RESTRICT"))
    operation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # True when the run covered the competitor's whole configured scope (required to
    # mark unseen categories/products as missing).
    full_scope: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    pages_fetched: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    pages_failed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    categories_seen: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    products_seen: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    products_created: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    products_updated: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    products_failed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    products_without_price: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    observations_created: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    categories_marked_missing: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    products_marked_missing: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    credits_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)

    competitor: Mapped[Competitor] = relationship(foreign_keys=[competitor_id])


class ScrapeRunItem(Base):
    __tablename__ = "scrape_run_items"
    __table_comment__ = "Per page/product outcome of a run (distinguishes failures from missing prices)"
    __table_args__ = (
        CheckConstraint(_in("item_type", ITEM_TYPES), name="ck_scrape_run_items_type"),
        CheckConstraint(_in("status", ITEM_STATUSES), name="ck_scrape_run_items_status"),
        Index("ix_scrape_run_items_run_status", "scrape_run_id", "status"),
        Index("ix_scrape_run_items_product", "product_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scrape_run_id: Mapped[int] = mapped_column(ForeignKey("scrape_runs.id", ondelete="CASCADE"))
    item_type: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    warnings: Mapped[list[Any] | None] = mapped_column(NullableJSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ListingPageCache(Base):
    """Listing pages fetched by the catalog sync, kept until a sync completes: a resumed sync
    replays them instead of fetching (and paying for) the category pages again."""

    __tablename__ = "listing_page_cache"
    __table_comment__ = "Listing pages of unfinished catalog syncs, replayed (no request) when a sync resumes"

    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"), primary_key=True)
    url: Mapped[str] = mapped_column(Text, primary_key=True, comment="requested URL")
    final_url: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scrape_run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id", ondelete="SET NULL"))
    body: Mapped[bytes] = mapped_column(LargeBinary, comment="zlib-compressed UTF-8 page text")


class ScrapeObservation(Base):
    """One observed price for one product and one quantity tier at one moment (append-only)."""

    __tablename__ = "scrape_observations"
    __table_comment__ = "Append-only history of observed prices, one row per product, tier and capture"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_obs_price_non_negative"),
        CheckConstraint(_in("price_basis", PRICE_BASES), name="ck_obs_price_basis"),
        CheckConstraint(_in("tax_mode", TAX_MODES), name="ck_obs_tax_mode"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_obs_currency"),
        CheckConstraint(
            "min_quantity IS NULL OR max_quantity IS NULL OR min_quantity <= max_quantity",
            name="ck_obs_quantity_band",
        ),
        Index("ix_obs_product_observed", "product_id", "observed_at"),
        Index("ix_obs_capture", "capture_id"),
        Index("ix_obs_run", "scrape_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    scrape_run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id", ondelete="SET NULL"))
    # All tiers read from the same page at the same time share one capture id.
    capture_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    min_quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    max_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric, comment="Upper bound of the band; NULL = open-ended. Exclusive unless max_inclusive"
    )
    max_inclusive: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    quantity_unit: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric)  # unbounded NUMERIC: exact, never rounded
    currency: Mapped[str] = mapped_column(String(3))
    price_basis: Mapped[str] = mapped_column(Text)
    price_unit: Mapped[str | None] = mapped_column(Text)
    price_unit_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric, comment="The price applies to this many price_unit (e.g. 1000 for '/ 1 000 feuilles'); NULL = unknown"
    )
    tax_mode: Mapped[str] = mapped_column(Text)
    is_promotional: Mapped[bool | None] = mapped_column(Boolean)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"))


class TextEmbedding(Base):
    """Embedding of one exact text, shared by every product (of any competitor) with that text.
    Never recomputed unless a run explicitly asks for it (``reembed``)."""

    __tablename__ = "text_embeddings"
    __table_comment__ = "Global embedding cache: one row per embedded text (model and dimensions in the hash)"

    text_hash: Mapped[str] = mapped_column(
        Text, primary_key=True, comment="sha256 of model, dimensions, input type and text"
    )
    model: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text, comment="the embedded text (null for rows migrated from 0005)")
    tokens: Mapped[int | None] = mapped_column(Integer, comment="tokens billed for this text (estimated share)")
    embedding: Mapped[bytes] = mapped_column(LargeBinary, comment="float32 little-endian, L2-normalized")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductEmbedding(Base):
    """The text embedded for each product: its vector is ``text_embeddings[text_hash]``."""

    __tablename__ = "product_embeddings"
    __table_comment__ = "Text embedded for each product (vector in text_embeddings)"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int] = mapped_column(Integer)
    text_hash: Mapped[str] = mapped_column(Text, comment="sha256 of model, dimensions, input type and text")
    tokens: Mapped[int | None] = mapped_column(
        Integer, comment="tokens billed when this product's text was embedded (null = reused)"
    )
    # Deprecated (0006): vectors live in text_embeddings. Kept nullable for the previous release
    # during a deploy; no longer written, dropped by a later migration.
    embedding: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, comment="deprecated: vectors are in text_embeddings (dropped by a later migration)"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductMatch(TimestampMixin, Base):
    """Current matching state of one competitor product against the reference catalog.

    Exactly one row per competitor product (reruns update it, never duplicate it). Every
    change is also appended to ``product_match_events``, so nothing is lost when a match is
    recomputed, decided by a human or deprecated.
    """

    __tablename__ = "product_matches"
    __table_comment__ = "Current association of each competitor product with a reference (Antalis) product"
    __table_args__ = (
        UniqueConstraint("product_id", name="uq_product_matches_product"),
        CheckConstraint(_in("status", MATCH_STATUSES), name="ck_product_matches_status"),
        CheckConstraint(_in("previous_status", MATCH_STATUSES), name="ck_product_matches_previous_status"),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="ck_product_matches_score"),
        CheckConstraint(
            "antalis_product_id IS NULL OR antalis_product_id <> product_id", name="ck_product_matches_not_self"
        ),
        Index("ix_product_matches_competitor_status", "competitor_id", "status"),
        Index("ix_product_matches_status_score", "status", "score"),
        Index("ix_product_matches_antalis_product", "antalis_product_id"),
        Index("ix_product_matches_proposed_product", "proposed_antalis_product_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text)
    antalis_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        comment="The associated reference product (auto_matched, validated); null otherwise",
    )
    proposed_antalis_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), comment="Best candidate found by the engine"
    )
    score: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), comment="Similarity score 0-100 of the proposed candidate (not a probability)"
    )
    runner_up_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    score_gap: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    dimensions_compared: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    decision_reasons: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # change detection
    product_hash: Mapped[str | None] = mapped_column(Text, comment="Hash of the competitor product's matching fields")
    antalis_product_hash: Mapped[str | None] = mapped_column(
        Text, comment="Hash of the associated (else proposed) reference product's matching fields"
    )
    reference_catalog_hash: Mapped[str | None] = mapped_column(Text, comment="Reference catalog state when computed")
    config_hash: Mapped[str | None] = mapped_column(Text, comment="Matching configuration when computed")
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("worker_jobs.id", ondelete="SET NULL"))
    # human decision
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column(Text)
    manual_choice: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), comment="validated with a product other than the engine's proposal"
    )
    rejected_antalis_product_ids: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), comment="Reference products a human rejected for this product"
    )
    # obsolescence
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deprecated_reason: Mapped[str | None] = mapped_column(Text)
    previous_status: Mapped[str | None] = mapped_column(Text, comment="Status before deprecation")
    revision: Mapped[int] = mapped_column(Integer, server_default=text("0"), comment="Optimistic lock")

    product: Mapped[Product] = relationship(foreign_keys=[product_id])
    antalis_product: Mapped[Product | None] = relationship(foreign_keys=[antalis_product_id])
    proposed_antalis_product: Mapped[Product | None] = relationship(foreign_keys=[proposed_antalis_product_id])


class ProductMatchCandidate(Base):
    """A reference product considered for a match, with its score breakdown (latest computation)."""

    __tablename__ = "product_match_candidates"
    __table_comment__ = "Ranked candidates of the latest computation of each match, with per-criterion scores"
    __table_args__ = (
        UniqueConstraint("match_id", "antalis_product_id", name="uq_product_match_candidates_pair"),
        Index("ix_product_match_candidates_antalis_product", "antalis_product_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("product_matches.id", ondelete="CASCADE"))
    antalis_product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    rank: Mapped[int | None] = mapped_column(Integer, comment="1 = best; null for excluded candidates")
    score: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    semantic_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    lexical_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    dimension_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), comment="null = not comparable")
    excluded: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class ProductMatchEvent(Base):
    """Append-only history of every match (computations, human decisions, deprecations)."""

    __tablename__ = "product_match_events"
    __table_comment__ = "Append-only history of product matches: computations, decisions, deprecations"
    __table_args__ = (
        CheckConstraint(_in("event", MATCH_EVENTS), name="ck_product_match_events_event"),
        Index("ix_product_match_events_match", "match_id", "created_at"),
        Index("ix_product_match_events_competitor", "competitor_id", "created_at"),
        Index("ix_product_match_events_event", "event", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("product_matches.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"))
    event: Mapped[str] = mapped_column(Text)
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str | None] = mapped_column(Text)
    # plain ids (no foreign key): the history survives the deletion of a reference product
    from_antalis_product_id: Mapped[int | None] = mapped_column(BigInteger)
    to_antalis_product_id: Mapped[int | None] = mapped_column(BigInteger)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    actor: Mapped[str] = mapped_column(Text, comment="'engine' or the name of the person who decided")
    reason: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("worker_jobs.id", ondelete="SET NULL"))
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), comment="Snapshot of the previous state (scores, top candidates)"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkerJob(Base):
    """One task (catalog sync or price refresh) executed by the worker for one competitor."""

    __tablename__ = "worker_jobs"
    __table_comment__ = "Worker tasks requested through the API; at most one active job per competitor"
    __table_args__ = (
        CheckConstraint(_in("task", JOB_TASKS), name="ck_worker_jobs_task"),
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_worker_jobs_status"),
        # The rule "never two workers on the same competitor" is enforced by the database.
        Index(
            "uq_worker_jobs_one_active_per_competitor",
            "competitor_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'paused')"),
        ),
        Index("ix_worker_jobs_competitor_created", "competitor_id", "created_at"),
        Index("ix_worker_jobs_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"))
    task: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    requested_by: Mapped[str] = mapped_column(Text, server_default=text("'api'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set = keep paused
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # when the worker paused
    claimed_by: Mapped[str | None] = mapped_column(Text)  # worker process instance id
    scrape_run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id", ondelete="SET NULL"))
    resumed_from_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("worker_jobs.id", ondelete="SET NULL", name="fk_worker_jobs_resumed_from"),
        comment="The interrupted/stopped job this one restarts (manual or automatic resume)",
    )
    step: Mapped[str | None] = mapped_column(Text)
    step_done: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    step_goal: Mapped[int | None] = mapped_column(Integer)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    counters: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    estimate: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)

    competitor: Mapped[Competitor] = relationship()


class Notification(Base):
    __tablename__ = "notifications"
    __table_comment__ = "Persisted notifications (job lifecycle, errors), read/unread"
    __table_args__ = (
        CheckConstraint(_in("level", NOTIFICATION_LEVELS), name="ck_notifications_level"),
        Index("ix_notifications_created", "created_at"),
        Index("ix_notifications_unread", "created_at", postgresql_where=text("read_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # job_started, job_succeeded, item_errors...
    title: Mapped[str] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    competitor_id: Mapped[int | None] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("worker_jobs.id", ondelete="SET NULL"))
    scrape_run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id", ondelete="SET NULL"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"
    __table_comment__ = "Liveness of long-running processes (the worker), shown by the API health endpoint"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    instance: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
