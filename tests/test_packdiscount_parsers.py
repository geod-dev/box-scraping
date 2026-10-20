"""Packdiscount adapter tests on saved Intershop API responses (no network)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from competitor_catalog.core.adapter import load_adapter
from competitor_catalog.core.config import load_competitor_config
from competitor_catalog.core.errors import UnexpectedPageError
from competitor_catalog.core.types import PageKind, PriceBasis, TaxMode
from competitor_catalog.settings import DEFAULT_COMPETITOR_CONFIG_DIR
from tests.conftest import fixture_text, make_page

API = "https://api.packdiscount.com/INTERSHOP/rest/WFS/Packdiscount-FR-Site/rest"
ROOT = f"{API}/categories"
CAT_2 = f"{API}/categories/2"
CAT_2_26 = f"{API}/categories/2/26"
EXPAND = "expand=prices%2Cimages%2Cattributes%2Cavailability%2Cvariations%2CvariableVariationAttributes"
MASTER = f"{API}/products/P00041?{EXPAND}"
VARIANT = f"{API}/products/MCFA23?{EXPAND}"
SIMPLE = f"{API}/products/263481?{EXPAND}"
PRODUCTS_0 = f"{API}/products?amount=200&offset=0"


@pytest.fixture(scope="module")
def adapter():
    return load_adapter(load_competitor_config(DEFAULT_COMPETITOR_CONFIG_DIR, "packdiscount_fr"))


def page(name: str, url: str):
    return make_page(fixture_text(f"packdiscount/{name}"), url)


def raw(name: str) -> dict:
    return json.loads(fixture_text(f"packdiscount/{name}"))


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize(
    ("name", "url", "kind"),
    [
        ("categories_root.json", ROOT, PageKind.LISTING),
        ("category_2_26.json", CAT_2_26, PageKind.LISTING),
        ("products_offset_0.json", PRODUCTS_0, PageKind.LISTING),
        ("product_master_P00041.json", MASTER, PageKind.LISTING),
        ("variations_P00041.json", f"{API}/products/P00041/variations?amount=200", PageKind.LISTING),
        ("product_variant_MCFA23.json", VARIANT, PageKind.PRODUCT),
        ("product_simple_263481.json", SIMPLE, PageKind.PRODUCT),
    ],
)
def test_classify(adapter, name, url, kind):
    assert adapter.classify(page(name, url)) is kind


@pytest.mark.parametrize("body", ["", "<html>Access denied</html>", "Not Found (Unable to find sub resource.)", "[]"])
def test_foreign_or_blocked_responses_are_rejected(adapter, body):
    with pytest.raises(UnexpectedPageError):
        adapter.classify(make_page(body, ROOT))
    with pytest.raises(UnexpectedPageError):
        adapter.parse_listing(make_page(body, ROOT))


# ---------------------------------------------------------------- navigation
def test_root_lists_top_level_categories(adapter):
    listing = adapter.parse_listing(page("categories_root.json", ROOT))
    assert listing.category is not None and listing.category.kind == "root"
    assert listing.category.ancestors == []
    assert len(listing.child_category_urls) == len(raw("categories_root.json")["elements"]) == 27
    assert all(url.startswith(f"{API}/categories/") for url in listing.child_category_urls)


def test_category_identity_is_its_id_path_and_ancestors_come_from_the_path(adapter):
    listing = adapter.parse_listing(page("category_2_26.json", CAT_2_26))
    cat = listing.category
    assert cat.url == CAT_2_26
    assert cat.external_id == "2/26"
    assert cat.name == "Carton simple cannelure"
    assert [a.url for a in cat.ancestors] == [ROOT, CAT_2]
    assert [a.name for a in cat.ancestors] == ["Catalogue Packdiscount", "Caisses Cartons et Boites"]
    assert listing.child_category_urls == []


def test_family_is_a_listing_whose_next_page_is_its_variations(adapter):
    listing = adapter.parse_listing(page("product_master_P00041.json", MASTER))
    family = listing.category
    assert family.kind == "product_family"
    assert family.url == MASTER
    assert family.external_id == "P00041"
    assert [a.url for a in family.ancestors] == [ROOT, CAT_2, CAT_2_26]
    assert listing.next_page_url == f"{API}/products/P00041/variations?amount=200"
    assert listing.product_links == []
    assert listing.warnings == []


def test_variations_page_gives_the_sellable_variants(adapter):
    listing = adapter.parse_listing(page("variations_P00041.json", f"{API}/products/P00041/variations?amount=200"))
    assert listing.category is None  # continuation: the engine keeps the family as owner
    refs = [link.reference for link in listing.product_links]
    assert refs == ["MCFA23", "MCFA24", "MCFA30", "MCFA31", "MCFA35"]
    assert listing.product_links[0].url == VARIANT


def test_flat_product_list_pages_through_the_whole_catalog(adapter):
    listing = adapter.parse_listing(page("products_offset_0.json", PRODUCTS_0))
    assert listing.category is None
    assert listing.expected_item_count == 1352
    assert len(listing.child_category_urls) == 200
    assert listing.next_page_url == f"{API}/products?amount=200&offset=200"
    assert listing.child_category_urls[0] == f"{API}/products/263481?{EXPAND}"


# ---------------------------------------------------------------- products
def test_variant_is_the_sellable_product(adapter):
    product = adapter.parse_product(page("product_variant_MCFA23.json", VARIANT))
    assert product.catalog_reference == "MCFA23"
    assert product.external_id == "MCFA23"
    assert product.url == VARIANT
    assert product.title.startswith("Caisse carton à fond automatique 30 x 30 x 23/30 cm")
    assert product.category.url == MASTER  # the family
    assert product.category.external_id == "P00041"
    assert [b.external_id for b in product.breadcrumb] == [None, "2", "2/26", "P00041"]
    assert product.breadcrumb[-1].url == MASTER
    assert product.brand == "Packdiscount"
    assert product.description == ""  # the short description is only the family link
    assert product.extra["product_master_sku"] == "P00041"
    assert product.extra["in_stock"] is True


def test_variant_dimensions_are_normalized_from_centimetres(adapter):
    product = adapter.parse_product(page("product_variant_MCFA23.json", VARIANT))
    assert product.normalized_attributes["inner_length_mm"] == 300
    assert product.normalized_attributes["inner_width_mm"] == 300
    assert product.normalized_attributes["inner_height_mm"] == 230
    assert product.normalized_attributes["max_height_mm"] == 300
    assert product.normalized_attributes["pallet_quantity"] == 720
    assert product.normalized_attributes["bundle_quantity"] == 20
    assert product.normalized_attributes["quality_code"] == "PC20"
    labels = {a["label"] for a in product.attributes if a["source"] == "variation"}
    assert "Longueur intérieure (cm)" in labels


def test_variant_price_is_per_unit_with_minimum_quantity(adapter):
    product = adapter.parse_product(page("product_variant_MCFA23.json", VARIANT))
    assert product.prices.price_found is True
    (tier,) = product.prices.tiers
    assert tier.price == Decimal("3.96")
    assert tier.currency == "EUR"
    assert tier.price_basis is PriceBasis.UNIT
    assert tier.tax_mode is TaxMode.UNKNOWN  # the API does not state it
    assert tier.min_quantity == Decimal("20.0")
    assert tier.max_quantity is None
    assert tier.quantity_unit == "UN" == tier.price_unit
    assert tier.is_promotional is False


def test_simple_product_is_stored_under_its_own_category(adapter):
    product = adapter.parse_product(page("product_simple_263481.json", SIMPLE))
    assert product.catalog_reference == "263481"
    assert product.category.url == f"{API}/categories/11/114"
    assert product.category.name == "Stylos"
    assert product.extra["product_master_sku"] is None
    (tier,) = product.prices.tiers
    assert tier.price == Decimal("3.56")
    assert tier.min_quantity == Decimal("6.0")


def test_prices_refresh_reads_the_same_fields(adapter):
    result = adapter.parse_prices(page("product_variant_MCFA23.json", VARIANT))
    assert result.catalog_reference == "MCFA23"
    assert result.url == VARIANT
    assert result.prices.tiers[0].price == Decimal("3.96")


def test_product_without_price_is_not_a_failure(adapter):
    data = raw("product_variant_MCFA23.json")
    data["salePrice"] = None
    product = adapter.parse_product(make_page(json.dumps(data), VARIANT))
    assert product.prices.price_found is False
    assert product.prices.tiers == []
    assert product.prices.reason == "no price shown"


def test_promotion_is_flagged_when_sale_price_is_below_list_price(adapter):
    data = raw("product_variant_MCFA23.json")
    data["listPrice"] = {"value": 4.5, "currency": "EUR"}
    product = adapter.parse_product(make_page(json.dumps(data), VARIANT))
    assert product.prices.tiers[0].is_promotional is True
    assert product.prices.tiers[0].metadata["list_price"] == 4.5


# ---------------------------------------------------------------- URLs
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{API}/products/MCFA23", VARIANT),
        (f"{API}/products/MCFA23?expand=prices", VARIANT),
        (f"{API}/products?amount=200&offset=0", PRODUCTS_0),
        (f"{API}/products/P00041/variations?amount=200", f"{API}/products/P00041/variations?amount=200"),
        (f"{API}/categories/2/26/", CAT_2_26),
    ],
)
def test_normalize_url(adapter, url, expected):
    assert adapter.normalize_url(url) == expected


def test_every_page_is_in_scope(adapter):
    assert adapter.is_in_scope(CAT_2_26, [], set()) is True


# ---------------------------------------------------------------- encoding
def test_utf8_text_survives_the_client_decoding():
    import httpx

    from competitor_catalog.http.scrapedo import _decode

    body = '{"name":"Caisse carton à fond"}'.encode()
    response = httpx.Response(200, content=body, headers={"Content-Type": "application/json"})
    assert json.loads(_decode(response))["name"] == "Caisse carton à fond"
