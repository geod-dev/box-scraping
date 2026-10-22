"""Cenpac France adapter: Intershop REST API for navigation, variant web pages for products.

* Navigation (JSON, ``categories``, ``products``, ``variations``): a category's identity is
  its id *path* joined with "/" and rooted at ``Cenpac`` (``Cenpac/1000/1004/1004a``).
* A category product list (``categories/<path>/products``) lists the families whose home
  category is in the category or below it, plus cross-listed families from other branches.
  Families outside the crawl scope (excluded top-level branches, or a home outside the
  ``Cenpac`` catalog) are rejected by :meth:`CenpacAdapter.is_in_scope`.
* A variation master (``VARIATION_MASTER``) is a family; its ``variations`` link lists the
  variant SKUs, each one sold on its own web page ``/-/-/p<sku>``.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from competitor_catalog.adapters.cenpac.prices import parse_price_tiers
from competitor_catalog.adapters.cenpac.variant_page import (
    BLOCK_MARKER,
    VariantPage,
    page_template,
    read_variant_page,
)
from competitor_catalog.core.adapter import CompetitorAdapter
from competitor_catalog.core.errors import PageParseError, UnexpectedPageError
from competitor_catalog.core.normalize import (
    build_attribute,
    build_normalized_attributes,
    clean_text,
    convert_unit,
    fold,
    parse_decimal,
    split_label_unit,
    to_json_number,
)
from competitor_catalog.core.types import (
    CategoryData,
    CategoryRef,
    ListingPage,
    PageKind,
    ProductData,
    ProductLink,
    ProductPricePage,
)
from competitor_catalog.core.urls import normalize_url
from competitor_catalog.http.base import FetchedPage

WEB_HOST = "www.cenpac.fr"
WEB_ROOT = "https://www.cenpac.fr"
WFS_ROOT = "https://www.cenpac.fr/INTERSHOP/rest/WFS/"
API_ROOT = f"{WFS_ROOT}Cenpac-Front-Site/-"
PAGE_SIZE = 200  # the API refuses amount > 200 (026)
ROOT_ID = "Cenpac"
ROOT_NAME = "Les produits"
MASTER_TYPE = "VARIATION_MASTER"
VARIANT_TEMPLATE = "Produit_sku"


def _q(value: str) -> str:
    return quote(value, safe="")


class CenpacAdapter(CompetitorAdapter):
    # ------------------------------------------------------------------ URLs
    def category_url(self, ids: list[str]) -> str:
        return normalize_url(f"{API_ROOT}/categories/" + "/".join(_q(i) for i in ids))

    def root_url(self) -> str:
        return self.category_url([ROOT_ID])

    def category_products_url(self, ids: list[str], offset: int = 0) -> str:
        joined = "/".join(_q(i) for i in ids)
        return normalize_url(f"{API_ROOT}/categories/{joined}/products?amount={PAGE_SIZE}&offset={offset}")

    def product_url(self, sku: str) -> str:
        """Family (variation master) URL. No ``expand`` parameter: the family is read as is."""
        return normalize_url(f"{API_ROOT}/products/{_q(sku)}")

    def variations_url(self, sku: str, offset: int = 0) -> str:
        base = f"{API_ROOT}/products/{_q(sku)}/variations?amount={PAGE_SIZE}"
        return normalize_url(base if offset == 0 else f"{base}&offset={offset}")

    def variant_url(self, sku: str) -> str:
        return self.normalize_url(f"{WEB_ROOT}/-/-/p{_q(sku)}")

    def normalize_url(self, url: str, base: str | None = None) -> str:
        normalized = super().normalize_url(url, base)
        parts = urlsplit(normalized)
        if parts.netloc == WEB_HOST and not parts.path.startswith("/INTERSHOP/"):
            # A SKU is never a lower-case word: "/promotions" is a page, not the variant "romotions".
            match = re.fullmatch(r"p(?![a-z]+\Z)(.+)", parts.path.rsplit("/", 1)[-1])
            if match:
                # A variant page: the query string and the family canonical path are dropped.
                return urlunsplit(("https", WEB_HOST, "/-/-/p" + match.group(1), "", ""))
        return normalized

    @property
    def excluded_top_level_ids(self) -> set[str]:
        return {str(x) for x in self.config.options.get("excluded_top_level_category_ids") or []}

    def category_ids(self, url: str) -> list[str] | None:
        """Id path of a category URL (root first), or None for a product URL or a product list."""
        prefix = urlsplit(API_ROOT).path + "/categories/"
        path = urlsplit(url).path
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix) :]
        if "/products" in rest:
            return None
        return [unquote(s) for s in rest.split("/") if s]

    def is_in_scope(self, category_url: str, ancestors_urls: list[str], root_urls: set[str]) -> bool:
        paths = [p for p in (self.category_ids(u) for u in [*ancestors_urls, category_url]) if p]
        if any(p[0] != ROOT_ID for p in paths):
            return False  # another catalog (NegotiatedPrices...)
        if any(len(p) >= 2 and p[1] in self.excluded_top_level_ids for p in paths):
            return False
        if self.category_ids(category_url) is None:  # a family (product URL)
            return any(len(p) >= 2 for p in paths)  # needs a home below the root
        return True

    # ------------------------------------------------------------------ contract
    def classify(self, page: FetchedPage) -> PageKind:
        if _is_json(page):
            data = self._load(page)
            resource = data["type"]
            if resource in ("Product", "Category", "ResourceCollection", "Variations"):
                return PageKind.LISTING  # a non-master Product is ignored by parse_listing
            raise UnexpectedPageError(f"unexpected Cenpac resource type {resource!r}", url=page.requested_url)
        if BLOCK_MARKER in page.text:
            raise UnexpectedPageError(
                "Cenpac access-denied page (blocked request or unknown SKU)", url=page.requested_url
            )
        template = page_template(page.text)
        if template == VARIANT_TEMPLATE:
            return PageKind.PRODUCT
        raise UnexpectedPageError(f"unexpected Cenpac page (template {template!r})", url=page.requested_url)

    def parse_listing(self, page: FetchedPage) -> ListingPage:
        data = self._load(page)
        resource = data["type"]
        if resource == "Category":
            return self._category(page, data)
        if resource == "Product":
            return self._family(page, data)
        if resource == "Variations":
            return self._variations(page, data)
        if resource == "ResourceCollection":
            return self._product_list(page, data)
        raise UnexpectedPageError(f"unexpected Cenpac resource type {resource!r}", url=page.requested_url)

    def parse_product(self, page: FetchedPage) -> ProductData:
        vp = self._variant(page)
        prices = parse_price_tiers(vp.doc, vp.sku, self.config.default_currency)
        attributes = [build_attribute("Caractéristiques", label, value) for label, value in vp.attributes]
        # Zero placeholders (e.g. "Longueur extérieure (mm) 0") never feed normalized values.
        usable = [a for a in attributes if a.get("numeric_value") != 0]
        normalized = build_normalized_attributes(usable, self.config.options.get("attribute_mapping") or {})
        dimensions = _split_dimensions(usable, self.config.options.get("dimension_attributes") or {})
        normalized.update({k: v for k, v in dimensions.items() if k not in normalized})

        warnings: list[str] = []
        master_ref = (
            CategoryRef(self.product_url(vp.master_sku), vp.family_name, vp.master_sku) if vp.master_sku else None
        )
        cats: list[CategoryRef] = []
        ids = [ROOT_ID, *(i for i, _ in vp.category_path)]
        if len(ids) >= 2:
            if ids[1] in self.excluded_top_level_ids:
                warnings.append(f"page category {ids[1:]} outside the crawl scope: breadcrumb limited to the family")
            else:
                cats = [
                    CategoryRef(
                        self.category_url(ids[: k + 1]),
                        vp.category_path[k - 1][1],
                        "/".join(ids[: k + 1]),
                    )
                    for k in range(1, len(ids))
                ]
        breadcrumb = [self._root_ref(), *cats, *([master_ref] if master_ref else [])]
        return ProductData(
            url=self.variant_url(vp.sku),
            catalog_reference=vp.sku,
            external_id=vp.sku,
            title=vp.title,
            description=vp.description,
            long_description=vp.long_description,
            brand=None,
            attributes=attributes,
            normalized_attributes=normalized,
            images=vp.images,
            category=master_ref,
            breadcrumb=breadcrumb,
            prices=prices,
            extra={
                "product_master_sku": vp.master_sku,
                "page_category_ids": [i for i, _ in vp.category_path],
                "min_order_quantity": prices.metadata.get("min_order_quantity"),
                "order_step": prices.metadata.get("order_step"),
                "sales_unit": prices.metadata.get("sales_unit"),
                "units_per_sales_unit": prices.metadata.get("units_per_sales_unit"),
            },
            warnings=warnings,
        )

    def parse_prices(self, page: FetchedPage) -> ProductPricePage:
        vp = self._variant(page)
        return ProductPricePage(
            url=self.variant_url(vp.sku),
            catalog_reference=vp.sku,
            prices=parse_price_tiers(vp.doc, vp.sku, self.config.default_currency),
        )

    # ------------------------------------------------------------------ parsing helpers
    @staticmethod
    def _load(page: FetchedPage) -> dict[str, Any]:
        try:
            data = json.loads(page.text)
        except ValueError as exc:
            raise UnexpectedPageError("not a Cenpac API response", url=page.requested_url) from exc
        if not isinstance(data, dict) or "type" not in data:
            raise UnexpectedPageError("unrecognised Cenpac API response", url=page.requested_url)
        return data

    @staticmethod
    def _variant(page: FetchedPage) -> VariantPage:
        if _is_json(page):
            raise UnexpectedPageError("not a Cenpac variant page", url=page.requested_url)
        return read_variant_page(page.text, page.requested_url)

    @staticmethod
    def _sku(uri: str) -> str:
        return unquote(uri.rstrip("/").rsplit("/", 1)[-1])

    def _root_ref(self) -> CategoryRef:
        return CategoryRef(url=self.root_url(), name=ROOT_NAME, external_id=ROOT_ID)

    def _chain(self, items: list[dict[str, Any]]) -> list[CategoryRef]:
        """Category path (root first) from ``categoryPath`` items, self included. The ``uri`` of
        these items is never read: the URL is rebuilt from the id path."""
        refs: list[CategoryRef] = []
        ids: list[str] = []
        for item in items:
            if item.get("id") is None:
                raise PageParseError("category path item without id")
            ids.append(str(item["id"]))
            refs.append(
                CategoryRef(
                    url=self.category_url(ids),
                    name=clean_text(item.get("name")),
                    external_id="/".join(ids),
                )
            )
        return refs

    # ------------------------------------------------------------------ listings
    def _category(self, page: FetchedPage, data: dict[str, Any]) -> ListingPage:
        chain = self._chain(data.get("categoryPath") or [])
        if not chain:
            raise PageParseError("category without categoryPath", url=page.requested_url)
        node = chain[-1]
        ids = (node.external_id or "").split("/")
        name = clean_text(data.get("name")) or node.name or ""
        children: list[str] = []
        for sub in data.get("subCategories") or []:
            if sub.get("id") is None:
                raise PageParseError("sub-category without id", url=page.requested_url)
            child_ids = [*ids, str(sub["id"])]
            if len(child_ids) >= 2 and child_ids[1] in self.excluded_top_level_ids:
                continue
            children.append(self.category_url(child_ids))
        if ids == [ROOT_ID]:
            # No product list for the root: it would list the masters of every branch.
            category = CategoryData(url=node.url, name=name, kind="root", external_id=ROOT_ID, ancestors=[])
        else:
            category = CategoryData(
                url=node.url,
                name=name,
                kind="category",
                external_id=node.external_id,
                ancestors=chain[:-1],
            )
            children.append(self.category_products_url(ids))
        return ListingPage(url=page.requested_url, category=category, child_category_urls=children)

    def _family(self, page: FetchedPage, data: dict[str, Any]) -> ListingPage:
        if MASTER_TYPE not in (data.get("productTypes") or []):
            return ListingPage(
                url=page.requested_url,
                category=None,
                warnings=[f"not a variation master ({data.get('productTypes')}): ignored"],
            )
        if data.get("sku") in (None, ""):
            raise UnexpectedPageError("variation master without a sku", url=page.requested_url)
        sku = str(data["sku"])
        chain = self._chain((data.get("defaultCategory") or {}).get("categoryPath") or [])
        category = CategoryData(
            url=self.product_url(sku),
            name=clean_text(data.get("name")) or sku,
            kind="product_family",
            external_id=sku,
            ancestors=chain or [self._root_ref()],
        )
        warnings: list[str] = []
        next_page = None
        if data.get("variations"):
            next_page = self.variations_url(sku)
        else:
            warnings.append("variation master without a variations link")
        return ListingPage(url=page.requested_url, category=category, next_page_url=next_page, warnings=warnings)

    def _variations(self, page: FetchedPage, data: dict[str, Any]) -> ListingPage:
        segments = [s for s in urlsplit(page.requested_url).path.split("/") if s]
        if "variations" not in segments or segments.index("variations") == 0:
            raise UnexpectedPageError("variations list without a product in the URL", url=page.requested_url)
        master = unquote(segments[segments.index("variations") - 1])
        offset = int(data.get("offset") or 0)
        amount = int(data.get("amount") or PAGE_SIZE)
        total = data.get("total")
        links = [
            ProductLink(self.variant_url(sku), reference=sku)
            for sku in (self._sku(e["uri"]) for e in data.get("elements") or [] if e.get("uri"))
        ]
        next_page = (
            self.variations_url(master, offset + amount) if total is not None and offset + amount < int(total) else None
        )
        return ListingPage(
            url=page.requested_url,
            category=None,
            product_links=links,
            next_page_url=next_page,
            expected_item_count=int(total) if total is not None else None,
        )

    def _product_list(self, page: FetchedPage, data: dict[str, Any]) -> ListingPage:
        path = urlsplit(page.requested_url).path
        if "/categories/" not in path:
            raise UnexpectedPageError("product list outside a category (flat list not used)", url=page.requested_url)
        segments = [s for s in path.split("/categories/", 1)[1].split("/") if s]
        if segments and segments[-1] == "products":
            segments = segments[:-1]
        ids = [unquote(s) for s in segments]
        children = [self.product_url(self._sku(e["uri"])) for e in data.get("elements") or [] if e.get("uri")]
        total = data.get("total")
        offset = int(data.get("offset") or 0)
        amount = int(data.get("amount") or PAGE_SIZE)
        next_page = (
            self.category_products_url(ids, offset + amount)
            if total is not None and offset + amount < int(total)
            else None
        )
        return ListingPage(
            url=page.requested_url,
            category=None,
            child_category_urls=children,
            next_page_url=next_page,
            expected_item_count=int(total) if total is not None else None,
            listed_in=self.category_url(ids),
        )


def _is_json(page: FetchedPage) -> bool:
    return page.text.lstrip()[:1] == "{"


def _split_dimensions(attributes: list[dict[str, Any]], spec: dict[str, list[str]]) -> dict[str, Any]:
    """Split "L x l x h" values into one normalized key per number (unit from the label).

    ``spec`` maps a page label to its keys, e.g. ``{"Dimensions intérieures Lxlxh (mm)":
    ["inner_length_mm", "inner_width_mm", "inner_height_mm"]}``. The first usable value wins.
    """
    result: dict[str, Any] = {}
    for label_spec, keys in spec.items():
        for attr in attributes:
            value = attr.get("value")
            if fold(str(attr.get("label", ""))) != fold(label_spec) or not value:
                continue
            numbers = [parse_decimal(part) for part in re.split(r"\s*[x×X]\s*", value.strip())]
            if len(numbers) != len(keys) or any(n is None or n <= 0 for n in numbers):
                continue
            unit = split_label_unit(str(attr["label"]))[1]
            converted = [
                convert_unit(n, unit, key.rsplit("_", 1)[-1])
                for n, key in zip(numbers, keys, strict=True)
                if n is not None
            ]
            if any(c is None for c in converted):
                continue
            result.update({key: to_json_number(c) for key, c in zip(keys, converted, strict=True) if c is not None})
            break
    return result
