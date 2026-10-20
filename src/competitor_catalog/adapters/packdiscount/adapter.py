"""Packdiscount France adapter: reads the Intershop REST API behind www.packdiscount.com.

The API is JSON, public and answers each request in one Scrape.do credit (no HTML parsing,
no JavaScript rendering). Model of the catalog, verified on saved responses:

* ``categories`` -> category tree. A category's identity is its id *path* (``2/26``), not its
  id: ids are only unique among siblings (``categories/26`` and ``categories/2/26`` differ).
* ``products`` (flat list, ``total`` = 1352) -> product masters and simple products. Variants
  are not in this list.
* A ``VARIATION_MASTER`` product is a family (kind ``product_family``, like Antalis families):
  it is not sold. Its ``variations`` link lists the sellable ``VARIATION_PRODUCT`` items, which
  carry the SKU, the price, the minimum order quantity and the dimensions.
* A simple ``PRODUCT`` is sold as it is.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from competitor_catalog.core.adapter import CompetitorAdapter
from competitor_catalog.core.errors import PageParseError, UnexpectedPageError
from competitor_catalog.core.html import block_text, parse_html
from competitor_catalog.core.normalize import build_attribute, build_normalized_attributes, clean_text
from competitor_catalog.core.types import (
    CategoryData,
    CategoryRef,
    ListingPage,
    PageKind,
    PriceBasis,
    PriceSnapshot,
    PriceTier,
    ProductData,
    ProductLink,
    ProductPricePage,
    TaxMode,
)
from competitor_catalog.core.urls import normalize_url
from competitor_catalog.http.base import FetchedPage

WFS_ROOT = "https://api.packdiscount.com/INTERSHOP/rest/WFS/"
API_ROOT = f"{WFS_ROOT}Packdiscount-FR-Site/rest"
PAGE_SIZE = 200  # the API refuses amount > 200
PRODUCT_EXPAND = "prices,images,attributes,availability,variations,variableVariationAttributes"
MASTER_TYPE = "VARIATION_MASTER"
ROOT_NAME = "Catalogue Packdiscount"
SKIPPED_ATTRIBUTES = frozenset({"shippingPriceDetails"})  # shipping grid, not a characteristic


class PackdiscountAdapter(CompetitorAdapter):
    # ------------------------------------------------------------------ URLs
    @staticmethod
    def product_url(sku: str) -> str:
        return normalize_url(f"{API_ROOT}/products/{sku}?expand={PRODUCT_EXPAND}")

    @staticmethod
    def category_url(id_path: list[str]) -> str:
        return normalize_url(f"{API_ROOT}/categories/" + "/".join(id_path))

    @staticmethod
    def root_category_url() -> str:
        return normalize_url(f"{API_ROOT}/categories")

    @staticmethod
    def product_list_url(offset: int = 0) -> str:
        return normalize_url(f"{API_ROOT}/products?amount={PAGE_SIZE}&offset={offset}")

    def normalize_url(self, url: str, base: str | None = None) -> str:
        normalized = super().normalize_url(url, base)
        parts = urlsplit(normalized)
        segments = parts.path.split("/")
        # .../rest/products/<sku> is a product detail: its expand parameter is part of the URL
        # the engine fetches and stores. The list (.../products) and .../products/<sku>/variations
        # do not match.
        if len(segments) >= 2 and segments[-2] == "products" and segments[-1]:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({"expand": PRODUCT_EXPAND}), ""))
        return normalized

    def is_in_scope(self, category_url: str, ancestors_urls: list[str], root_urls: set[str]) -> bool:
        # The whole catalog is crawled: every category is under the root category.
        return True

    # ------------------------------------------------------------------ parsing helpers
    @staticmethod
    def _load(page: FetchedPage) -> dict[str, Any]:
        try:
            data = json.loads(page.text)
        except ValueError as exc:
            raise UnexpectedPageError("not a Packdiscount API response", url=page.requested_url) from exc
        if not isinstance(data, dict) or "type" not in data:
            raise UnexpectedPageError("unrecognised Packdiscount API response", url=page.requested_url)
        return data

    @staticmethod
    def _absolute(uri: str) -> str:
        return normalize_url(f"{WFS_ROOT}{uri.lstrip('/')}")

    @staticmethod
    def _sku(uri: str) -> str:
        return uri.rstrip("/").rsplit("/", 1)[-1]

    def _root_ref(self) -> CategoryRef:
        return CategoryRef(url=self.root_category_url(), name=ROOT_NAME)

    def _chain(self, path_items: list[dict[str, Any]]) -> list[CategoryRef]:
        """Category path (root first) from Intershop ``categoryPath`` items, self included.

        The ``uri`` of these items is not trusted (it is wrong for nested nodes in some
        responses); the URL is rebuilt from the id path instead."""
        refs: list[CategoryRef] = []
        ids: list[str] = []
        for item in path_items:
            ids.append(str(item["id"]))
            refs.append(
                CategoryRef(
                    url=self.category_url(ids),
                    name=clean_text(item.get("name")),
                    external_id="/".join(ids),
                )
            )
        return refs

    # ------------------------------------------------------------------ contract
    def classify(self, page: FetchedPage) -> PageKind:
        data = self._load(page)
        resource = data["type"]
        if resource == "Product":
            return PageKind.LISTING if MASTER_TYPE in (data.get("productTypes") or []) else PageKind.PRODUCT
        if resource in ("Categories", "Category", "ResourceCollection", "Variations"):
            return PageKind.LISTING
        raise UnexpectedPageError(f"unexpected Intershop resource type {resource!r}", url=page.requested_url)

    def parse_listing(self, page: FetchedPage) -> ListingPage:
        data = self._load(page)
        resource = data["type"]
        if resource == "Categories":
            return self._root(page, data)
        if resource == "Category":
            return self._category(page, data)
        if resource == "Product":
            return self._family(page, data)
        if resource == "Variations":
            return self._variations(page, data)
        if resource == "ResourceCollection":
            return self._product_list(page, data)
        raise UnexpectedPageError(f"unexpected Intershop resource type {resource!r}", url=page.requested_url)

    def parse_product(self, page: FetchedPage) -> ProductData:
        data = self._product_resource(page)
        sku = str(data["sku"])
        chain = self._chain((data.get("defaultCategory") or {}).get("categoryPath") or [])
        master_sku = data.get("productMasterSKU")
        master_ref = (
            CategoryRef(url=self.product_url(master_sku), name=self._master_name(data), external_id=master_sku)
            if master_sku
            else None
        )
        category = master_ref or (chain[-1] if chain else None)
        breadcrumb = [self._root_ref(), *chain] + ([master_ref] if master_ref else [])
        attributes = self._attributes(data)
        mapping = self.config.options.get("attribute_mapping") or {}
        return ProductData(
            url=self.product_url(sku),
            catalog_reference=sku,
            external_id=sku,
            title=clean_text(data.get("name")),
            description=_html_text(data.get("shortDescription")),
            long_description=_html_text(data.get("longDescription")),
            brand=clean_text(data.get("manufacturer")),
            attributes=attributes,
            normalized_attributes=build_normalized_attributes(attributes, mapping),
            images=self._images(data),
            category=category,
            breadcrumb=breadcrumb,
            prices=self._prices(data),
            extra={
                "in_stock": data.get("inStock"),
                "product_types": data.get("productTypes") or [],
                "product_master_sku": master_sku,
                "packing_unit": clean_text(data.get("packingUnit")),
                "supplier_sku": clean_text(data.get("supplierSKU")),
            },
        )

    def parse_prices(self, page: FetchedPage) -> ProductPricePage:
        data = self._product_resource(page)
        sku = str(data["sku"])
        return ProductPricePage(url=self.product_url(sku), catalog_reference=sku, prices=self._prices(data))

    # ------------------------------------------------------------------ listings
    def _root(self, page: FetchedPage, data: dict[str, Any]) -> ListingPage:
        children = [self._absolute(e["uri"]) for e in data.get("elements", []) if e.get("uri")]
        return ListingPage(
            url=page.final_url,
            category=CategoryData(url=self.root_category_url(), name=ROOT_NAME, kind="root"),
            child_category_urls=children,
        )

    def _category(self, page: FetchedPage, data: dict[str, Any]) -> ListingPage:
        path = data.get("categoryPath") or []
        chain = self._chain(path)
        if not chain:
            raise PageParseError("category without categoryPath", url=page.requested_url)
        node = chain[-1]
        category = CategoryData(
            url=node.url,
            name=clean_text(data.get("name")) or node.name or "",
            kind="category",
            external_id=node.external_id,
            ancestors=[self._root_ref(), *chain[:-1]],
        )
        children = [self._absolute(s["uri"]) for s in data.get("subCategories") or [] if s.get("uri")]
        return ListingPage(url=page.final_url, category=category, child_category_urls=children)

    def _family(self, page: FetchedPage, data: dict[str, Any]) -> ListingPage:
        """A variation master: a family, not a sellable product. Its variants are the next page."""
        sku = str(data["sku"])
        chain = self._chain((data.get("defaultCategory") or {}).get("categoryPath") or [])
        warnings: list[str] = []
        next_page = None
        if data.get("variations"):
            next_page = normalize_url(f"{self._absolute(data['variations'])}?amount={PAGE_SIZE}")
        else:
            warnings.append("variation master without a variations link")
        category = CategoryData(
            url=self.product_url(sku),
            name=clean_text(data.get("name")) or sku,
            kind="product_family",
            external_id=sku,
            ancestors=[self._root_ref(), *chain],
        )
        return ListingPage(url=page.final_url, category=category, next_page_url=next_page, warnings=warnings)

    def _variations(self, page: FetchedPage, data: dict[str, Any]) -> ListingPage:
        elements = data.get("elements") or []
        warnings = ["variations list may be truncated (page size reached)"] if len(elements) >= PAGE_SIZE else []
        links = [
            ProductLink(url=self.product_url(self._sku(e["uri"])), reference=self._sku(e["uri"]))
            for e in elements
            if e.get("uri")
        ]
        return ListingPage(url=page.final_url, category=None, product_links=links, warnings=warnings)

    def _product_list(self, page: FetchedPage, data: dict[str, Any]) -> ListingPage:
        """Flat product list: masters and simple products (variants are not listed here)."""
        total = data.get("total")
        offset = int(data.get("offset") or 0)
        amount = int(data.get("amount") or PAGE_SIZE)
        children = [self.product_url(self._sku(e["uri"])) for e in data.get("elements", []) if e.get("uri")]
        next_page = (
            self.product_list_url(offset + amount) if total is not None and offset + amount < int(total) else None
        )
        return ListingPage(
            url=page.final_url,
            category=None,
            child_category_urls=children,
            next_page_url=next_page,
            expected_item_count=int(total) if total is not None else None,
        )

    # ------------------------------------------------------------------ products
    def _product_resource(self, page: FetchedPage) -> dict[str, Any]:
        data = self._load(page)
        if data["type"] != "Product" or not data.get("sku"):
            raise UnexpectedPageError("not a Packdiscount product", url=page.requested_url)
        return data

    @staticmethod
    def _master_name(data: dict[str, Any]) -> str | None:
        for attr in data.get("attributes") or []:
            if attr.get("name") == "productMasterDisplayName":
                return clean_text(str(attr.get("value") or "")) or None
        return None

    def _prices(self, data: dict[str, Any]) -> PriceSnapshot:
        sale = data.get("salePrice") or {}
        if sale.get("value") is None:
            return PriceSnapshot(tiers=[], price_found=False, reason="no price shown")
        currency = sale.get("currency") or sale.get("currencyMnemonic") or "EUR"
        if not re.fullmatch(r"[A-Za-z]{3}", currency):
            # Intershop shows a product without price as {"value": 0.0, "currency": "N/A"}.
            return PriceSnapshot(
                tiers=[], price_found=False, reason=f"no price shown (placeholder price, currency {currency!r})"
            )
        list_price = (data.get("listPrice") or {}).get("value")
        unit = clean_text(data.get("packingUnit")) or "unit"
        min_qty = data.get("minOrderQuantity")
        tier = PriceTier(
            price=Decimal(str(sale["value"])),
            currency=currency,
            price_basis=PriceBasis.UNIT,
            # The API does not say whether prices include VAT: kept unknown until verified.
            tax_mode=TaxMode.UNKNOWN,
            min_quantity=Decimal(str(min_qty)) if min_qty is not None else None,
            quantity_unit=unit,
            price_unit=unit,
            is_promotional=None if list_price is None else Decimal(str(sale["value"])) < Decimal(str(list_price)),
            metadata={
                "list_price": list_price,
                "step_order_quantity": data.get("stepOrderQuantity"),
                "max_order_quantity": data.get("maxOrderQuantity"),
            },
        )
        return PriceSnapshot(tiers=[tier], price_found=True)

    def _attributes(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        labels = self.config.options.get("variation_attribute_labels") or {}
        items: list[dict[str, Any]] = []
        for attr in data.get("variableVariationAttributes") or []:
            code = attr.get("variationAttributeId")
            label = labels.get(code) or attr.get("name") or code or ""
            items.append(build_attribute("Caractéristiques", label, _text(attr.get("value"))) | {"source": "variation"})
        for attr in data.get("attributes") or []:
            name = attr.get("name")
            if not name or name in SKIPPED_ATTRIBUTES or attr.get("type") == "ArrayList":
                continue
            items.append(build_attribute("Attributs", name, _text(attr.get("value"))) | {"source": "attribute"})
        return items

    @staticmethod
    def _images(data: dict[str, Any]) -> list[dict[str, Any]] | None:
        images = data.get("images")
        if images is None:
            return None
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for img in images:
            url = img.get("effectiveUrl")
            if not url or url in seen:
                continue
            seen.add(url)
            result.append({"url": url, "view": img.get("viewID"), "type": img.get("typeID")})
        return result


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "oui" if value else "non"
    return str(value)


def _html_text(value: str | None) -> str | None:
    """Text of an HTML fragment, without the "see all references" link of family pages."""
    if not value:
        return None
    text = block_text(parse_html(f"<div>{value}</div>")) or ""
    lines = [line for line in text.splitlines() if "Voir toutes les références" not in line]
    return "\n".join(lines).strip()  # "" = the page confirms there is no description
