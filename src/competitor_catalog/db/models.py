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
MATCH_STATUSES = ("proposed", "validated", "rejected")


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
    antalis_reference: Mapped[str | None] = mapped_column(
        Text, comment="Corresponding Antalis reference (own reference for Antalis, cross-reference for others)"
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

    competitor: Mapped[Competitor] = relationship()


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


class ProductMatch(TimestampMixin, Base):
    """Proposed/validated equivalence between two competitor products (never a merge)."""

    __tablename__ = "product_matches"
    __table_comment__ = "Proposed/validated equivalences between products of different competitors"
    __table_args__ = (
        UniqueConstraint("product_a_id", "product_b_id", name="uq_product_matches_pair"),
        CheckConstraint("product_a_id < product_b_id", name="ck_product_matches_ordered_pair"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_product_matches_conf"),
        CheckConstraint(_in("status", MATCH_STATUSES), name="ck_product_matches_status"),
        Index("ix_product_matches_b", "product_b_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_a_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    product_b_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    match_type: Mapped[str] = mapped_column(Text, server_default=text("'equivalent'"))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    status: Mapped[str] = mapped_column(Text, server_default=text("'proposed'"))
    method: Mapped[str | None] = mapped_column(Text)  # manual, antalis_reference, attribute_rule...
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    validated_by: Mapped[str | None] = mapped_column(Text)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
