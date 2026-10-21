"""Data exchanged between competitor adapters and the generic engine.

Convention for "missing vs empty": ``None`` always means *not observed* (section absent,
parse failure...). An empty list/dict/string means the page *confirmed* there is nothing.
Persistence never overwrites a known value with ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class PageKind(StrEnum):
    LISTING = "listing"  # a navigation node: category / listing / product family page
    PRODUCT = "product"  # a product (SKU) detail page


class PriceBasis(StrEnum):
    UNIT = "unit"  # price of one ``price_unit``
    TOTAL = "total"  # total price for the whole tier quantity
    UNKNOWN = "unknown"


class TaxMode(StrEnum):
    EXCL_TAX = "excl_tax"
    INCL_TAX = "incl_tax"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CategoryRef:
    """Pointer to a category as seen from another page (breadcrumb item, product page)."""

    url: str | None
    name: str | None = None
    external_id: str | None = None


@dataclass
class CategoryData:
    url: str  # canonical URL of the category page
    name: str
    kind: str  # competitor-specific node type, e.g. "sub_category", "product_family"
    external_id: str | None = None  # only when the site exposes a real identifier
    ancestors: list[CategoryRef] = field(default_factory=list)  # root first, parent last
    title: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def parent(self) -> CategoryRef | None:
        return self.ancestors[-1] if self.ancestors else None


@dataclass(frozen=True)
class ProductLink:
    url: str
    reference: str | None = None  # catalog reference when the listing exposes it


@dataclass
class ListingPage:
    url: str
    category: CategoryData | None
    child_category_urls: list[str] = field(default_factory=list)
    product_links: list[ProductLink] = field(default_factory=list)
    next_page_url: str | None = None
    # Total number of items announced by the site for the whole listing (all pages);
    # used to detect incomplete pagination.
    expected_item_count: int | None = None
    # Listing pages needed only by a crawl limited to part of the catalog (e.g. one category):
    # the products of this category when the full crawl reaches them another way (a flat list).
    scoped_child_urls: list[str] = field(default_factory=list)
    # URL of the category whose items this page lists, when the page is not the category page
    # itself (e.g. a separate product list endpoint). The items of a category page (and of its
    # pagination pages) are its listing already.
    listed_in: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class PriceTier:
    price: Decimal
    currency: str
    price_basis: PriceBasis
    tax_mode: TaxMode
    quantity: Decimal | None = None  # exact quantity tier
    min_quantity: Decimal | None = None  # band lower bound (inclusive); None = no lower bound
    max_quantity: Decimal | None = None  # band upper bound; None = open-ended
    max_inclusive: bool = False  # False: [min, max) ; True: [min, max]
    quantity_unit: str | None = None  # unit in which quantity bounds are expressed
    price_unit: str | None = None  # unit the price refers to (when basis = unit)
    price_unit_quantity: Decimal | None = None  # price is for this many price units ("/ 1 000 feuilles")
    is_promotional: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PriceSnapshot:
    """All prices observed on one page at one moment.

    ``tiers == []`` with ``price_found=False`` means the page was parsed successfully but
    shows no price (e.g. "price on request"): that is *not* a scrape failure.
    """

    tiers: list[PriceTier]
    price_found: bool
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProductData:
    url: str  # canonical product detail URL
    catalog_reference: str | None
    prices: PriceSnapshot
    external_id: str | None = None
    title: str | None = None
    description: str | None = None
    long_description: str | None = None
    brand: str | None = None
    attributes: list[dict[str, Any]] | None = None
    normalized_attributes: dict[str, Any] | None = None
    images: list[dict[str, Any]] | None = None
    category: CategoryRef | None = None  # most specific category / family
    breadcrumb: list[CategoryRef] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProductPricePage:
    """Result of the price-only parse: identity (to detect redirects) + prices."""

    url: str
    catalog_reference: str | None
    prices: PriceSnapshot
