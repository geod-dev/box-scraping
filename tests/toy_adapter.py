"""A minimal second competitor used to test the generic engine.

Pages are JSON documents, which keeps engine tests independent from any HTML layout. It
also shows how little is needed to plug a competitor into the engine.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from competitor_catalog.core.adapter import CompetitorAdapter
from competitor_catalog.core.errors import UnexpectedPageError
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
from competitor_catalog.http.base import FetchedPage

BASE = "https://shop.toy.test"


class ToyAdapter(CompetitorAdapter):
    def _load(self, page: FetchedPage) -> dict[str, Any]:
        try:
            return json.loads(page.text)
        except ValueError as exc:
            raise UnexpectedPageError("not a toy page", url=page.requested_url) from exc

    def classify(self, page: FetchedPage) -> PageKind:
        return PageKind(self._load(page)["type"])

    def parse_listing(self, page: FetchedPage) -> ListingPage:
        data = self._load(page)
        cat = data.get("category")  # absent on a separate list page ("listed_in")
        return ListingPage(
            url=page.final_url,
            category=CategoryData(
                url=cat["url"],
                name=cat["name"],
                kind=cat.get("kind", "category"),
                external_id=cat.get("id"),
                ancestors=[CategoryRef(url=u) for u in cat.get("ancestors", [])],
            )
            if cat
            else None,
            child_category_urls=data.get("children", []),
            product_links=[ProductLink(url=p["url"], reference=p.get("ref")) for p in data.get("products", [])],
            next_page_url=data.get("next"),
            expected_item_count=data.get("expected"),
            scoped_child_urls=data.get("scoped", []),
            listed_in=data.get("listed_in"),
        )

    def _prices(self, data: dict[str, Any]) -> PriceSnapshot:
        tiers = [
            PriceTier(
                price=Decimal(str(t["price"])),
                currency="EUR",
                price_basis=PriceBasis.UNIT,
                tax_mode=TaxMode.EXCL_TAX,
                min_quantity=Decimal(str(t["min"])) if t.get("min") is not None else None,
                max_quantity=Decimal(str(t["max"])) if t.get("max") is not None else None,
                quantity_unit="unit",
                price_unit="unit",
            )
            for t in data.get("tiers", [])
        ]
        return PriceSnapshot(tiers=tiers, price_found=bool(tiers), reason=None if tiers else "no price shown")

    def parse_product(self, page: FetchedPage) -> ProductData:
        data = self._load(page)
        return ProductData(
            url=data["url"],
            catalog_reference=data.get("ref"),
            external_id=data.get("ref"),
            title=data.get("title"),
            description=data.get("description"),
            attributes=data.get("attributes"),
            normalized_attributes=data.get("normalized"),
            images=data.get("images"),
            category=CategoryRef(url=data.get("category_url"), external_id=data.get("category_id")),
            prices=self._prices(data),
        )

    def parse_prices(self, page: FetchedPage) -> ProductPricePage:
        data = self._load(page)
        return ProductPricePage(url=data["url"], catalog_reference=data.get("ref"), prices=self._prices(data))


def toy_site(products: dict[str, dict[str, Any]] | None = None, *, page_size: int = 2) -> dict[str, str]:
    """root -> 2 sub categories ; sub A paginated product listing ; sub B empty-ish leaf."""
    products = (
        products
        if products is not None
        else {
            "A1": {"price": 10, "category": "A"},
            "A2": {"price": 20, "category": "A"},
            "A3": {"price": 30, "category": "A"},
            "B1": {"price": 5, "category": "B"},
        }
    )
    root, cat_a, cat_b = f"{BASE}/root", f"{BASE}/cat-a", f"{BASE}/cat-b"
    pages: dict[str, Any] = {
        root: {
            "type": "listing",
            "category": {"url": root, "name": "Root", "kind": "root"},
            "children": [cat_a, cat_b, cat_a],  # duplicate link on purpose
        },
        cat_b: {
            "type": "listing",
            "category": {"url": cat_b, "name": "B", "id": "CB", "ancestors": [root]},
            "products": [{"url": f"{BASE}/p/{ref}", "ref": ref} for ref, p in products.items() if p["category"] == "B"],
        },
    }
    refs_a = [ref for ref, p in products.items() if p["category"] == "A"]
    chunks = [refs_a[i : i + page_size] for i in range(0, len(refs_a), page_size)] or [[]]
    for index, chunk in enumerate(chunks):
        url = cat_a if index == 0 else f"{cat_a}?page={index + 1}"
        pages[url] = {
            "type": "listing",
            "category": {"url": url, "name": "A", "id": "CA", "ancestors": [root]},
            "products": [{"url": f"{BASE}/p/{ref}", "ref": ref} for ref in chunk],
            "expected": len(refs_a),
            "next": f"{cat_a}?page={index + 2}" if index + 1 < len(chunks) else None,
        }
    for ref, p in products.items():
        cat_url, cat_id = (cat_a, "CA") if p["category"] == "A" else (cat_b, "CB")
        tiers = p.get("tiers", [{"price": p["price"], "min": 1}] if p.get("price") is not None else [])
        pages[f"{BASE}/p/{ref}"] = {
            "type": "product",
            "url": f"{BASE}/p/{ref}",
            "ref": ref,
            "title": p.get("title", f"Product {ref}"),
            "description": p.get("description", f"Description {ref}"),
            "attributes": p.get("attributes", [{"label": "Length (mm)", "value": "100"}]),
            "normalized": {"length_mm": 100},
            "images": [{"url": f"{BASE}/img/{ref}.jpg"}],
            "category_url": cat_url,
            "category_id": cat_id,
            "tiers": tiers,
        }
    return {url: json.dumps(page) for url, page in pages.items()}


PROMO, PALLETS, PALLETS_LIST = f"{BASE}/promo", f"{BASE}/promo/pallets", f"{BASE}/promo/pallets/list"


def add_extra_categories(site: dict[str, str]) -> dict[str, str]:
    """Adds two extra categories, which own no product: "Promo" lists A1 and A2 on its own page,
    its sub-category "Pallets" lists A1 and the whole category B on a separate list page."""
    pages = {url: json.loads(body) for url, body in site.items()}
    root, cat_b = f"{BASE}/root", f"{BASE}/cat-b"
    pages[root]["children"].append(PROMO)
    pages[PROMO] = {
        "type": "listing",
        "category": {"url": PROMO, "name": "Promo", "id": "CP", "ancestors": [root]},
        "children": [PALLETS],
        "products": [{"url": f"{BASE}/p/{ref}", "ref": ref} for ref in ("A1", "A2")],
    }
    pages[PALLETS] = {
        "type": "listing",
        "category": {"url": PALLETS, "name": "Pallets", "id": "CPP", "ancestors": [root, PROMO]},
        "children": [PALLETS_LIST],
    }
    pages[PALLETS_LIST] = {
        "type": "listing",
        "listed_in": PALLETS,
        "children": [cat_b],
        "products": [{"url": f"{BASE}/p/A1", "ref": "A1"}],
    }
    return {url: json.dumps(page) for url, page in pages.items()}
