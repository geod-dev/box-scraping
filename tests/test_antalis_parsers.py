"""Antalis adapter tests on saved pages (no network)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from competitor_catalog.core.adapter import load_adapter
from competitor_catalog.core.config import load_competitor_config
from competitor_catalog.core.errors import PageParseError, UnexpectedPageError
from competitor_catalog.core.types import PageKind, PriceBasis, TaxMode
from competitor_catalog.settings import DEFAULT_COMPETITOR_CONFIG_DIR
from tests.conftest import fixture_text, make_page

BASE = "https://www.antalis.fr/eshop/packaging-machines-emballage"
FAMILY_HQ02953 = (
    f"{BASE}/caisses-carton-palettes/caisse-carton-ondule/"
    "caisse-carton-a-hauteur-variable-a-fond-automatique/pdp-hq02953"
)
SKU_503623 = (
    f"{BASE}/caisses-carton-palettes/caisse-carton-a-fond-automatique-hauteur-variable-simple-cannelure-"
    "305-x-215-x-170-mm-pdp-hq02953/sku-503623"
)


@pytest.fixture(scope="module")
def adapter():
    return load_adapter(load_competitor_config(DEFAULT_COMPETITOR_CONFIG_DIR, "antalis_fr"))


def page(name: str, url: str = "https://www.antalis.fr/eshop/x"):
    return make_page(fixture_text(f"antalis/{name}"), url)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("sector_cat-3.html", PageKind.LISTING),
        ("sub_hq02946.html", PageKind.LISTING),
        ("plp_hq02947.html", PageKind.LISTING),
        ("family_hq02953.html", PageKind.LISTING),
        ("sku_503623.html", PageKind.PRODUCT),
    ],
)
def test_classify_uses_page_context(adapter, name, kind):
    assert adapter.classify(page(name)) is kind


@pytest.mark.parametrize(
    "body", ["", "<html><body>Access Denied</body></html>", "<html><body><h1>Oops</h1></body></html>"]
)
def test_blocked_or_foreign_pages_are_rejected_not_treated_as_empty(adapter, body):
    with pytest.raises(UnexpectedPageError):
        adapter.classify(make_page(body))
    with pytest.raises(UnexpectedPageError):
        adapter.parse_listing(make_page(body))


def test_sector_root(adapter):
    listing = adapter.parse_listing(page("sector_cat-3.html"))
    cat = listing.category
    assert cat.url == f"{BASE}/cat-3"  # canonical URL after the 301
    assert cat.name == "Packaging et Machines d'emballage"
    assert cat.kind == "sector"
    assert cat.external_id is None  # HQ00000 is a placeholder, not an id
    assert cat.ancestors == []
    children = listing.child_category_urls
    assert len(children) == len(set(children)) == 77
    assert f"{BASE}/caisses-carton-palettes/sub-hq02946" in children
    assert all(u.startswith("https://www.antalis.fr/eshop/") for u in children)
    # promotions / editorial / home links are not navigation children
    assert not any("/ws/" in u or u.endswith("/eshop/home") for u in children)
    assert listing.product_links == []


def test_sub_category_hierarchy_from_breadcrumb(adapter):
    listing = adapter.parse_listing(page("sub_hq02946.html"))
    cat = listing.category
    assert (cat.name, cat.kind, cat.external_id) == ("Caisses Carton & Palettes", "sub_category", "HQ02946")
    assert [a.url for a in cat.ancestors] == [f"{BASE}/cat-3"]
    assert f"{BASE}/caisses-carton-palettes/caisse-carton-ondule/plp-hq02947" in listing.child_category_urls
    # SEO text links (boite-carton/plp-hq02961) are not part of the navigation grid
    assert not any("plp-hq02961" in u for u in listing.child_category_urls)


def test_product_listing_page(adapter):
    listing = adapter.parse_listing(page("plp_hq02947.html"))
    assert listing.category.kind == "product_listing"
    assert listing.category.external_id == "HQ02947"
    assert listing.expected_item_count == 14
    assert len(listing.child_category_urls) == 14
    assert FAMILY_HQ02953 in listing.child_category_urls
    assert listing.next_page_url is None


def test_family_page_lists_skus_and_paginates(adapter):
    p1 = adapter.parse_listing(page("family_hq02957_p1.html"))
    assert p1.category.kind == "product_family"
    assert p1.category.external_id == "HQ02957"
    assert p1.expected_item_count == 132
    assert len(p1.product_links) == 20
    assert all(
        "/sku-" in link.url and link.reference and link.url.endswith(link.reference) for link in p1.product_links
    )
    assert p1.next_page_url.endswith("pdp-hq02957?no=20")

    p2 = adapter.parse_listing(page("family_hq02957_p2.html"))
    assert p2.next_page_url.endswith("pdp-hq02957?no=40")
    assert len(p2.product_links) == 20
    assert not {link.reference for link in p1.product_links} & {link.reference for link in p2.product_links}


def test_small_family(adapter):
    listing = adapter.parse_listing(page("family_hq02953.html"))
    assert listing.category.url == FAMILY_HQ02953
    assert [a.name for a in listing.category.ancestors] == [
        "Packaging et Machines d'emballage",
        "Caisses Carton & Palettes",
        "Caisse Carton Ondulé",
    ]
    assert sorted(link.reference for link in listing.product_links) == ["348426", "503623", "506211", "506212"]
    assert SKU_503623 in [link.url for link in listing.product_links]


def test_sku_503623_full_parse(adapter):
    product = adapter.parse_product(page("sku_503623.html"))
    assert product.url == SKU_503623
    assert product.catalog_reference == product.external_id == "503623"
    assert product.title == "Caisse carton à fond automatique et hauteur variable simple cannelure 305 x 215 x 170 mm"
    assert product.description.startswith("Caisse carton à fond automatique et hauteur variable simple cannelure, RA4")
    assert "rainurage tous les centimètres" in product.long_description
    assert "Certificat environnemental" not in product.long_description
    assert product.brand == "Sans marque"

    # category association = the family page, identified by the id exposed by the site
    assert product.category.external_id == "HQ02953"
    assert product.category.url == FAMILY_HQ02953
    assert [b.name for b in product.breadcrumb][-2:] == [
        "Caisse Carton Ondulé",
        "Caisse Carton à hauteur variable et à fond automatique",
    ]

    attrs = {(a["group"], a["label"]): a for a in product.attributes}
    assert attrs[("Dimensions", "Longueur (mm)")]["value"] == "305"
    assert attrs[("Dimensions", "Longueur (mm)")]["unit"] == "mm"
    assert attrs[("Dimensions", "Longueur (mm)")]["numeric_value"] == 305
    assert attrs[("Composition", "Type de cannelure")]["value"] == "cannelure B"
    assert attrs[("Général", "Unité d'emballage")]["value"] == "paquet de 10 unités"
    assert attrs[("Informations techniques", "Stockage")]["value"] == "Stocker à plat, dans un endroit sec"
    assert {a["value"] for a in product.attributes if a["source"] == "pictogram"} == {
        "Recyclé",
        "Fabriqué à partir de Recyclé",
    }
    assert len([a for a in product.attributes if a["source"] == "characteristics"]) == 15

    norm = product.normalized_attributes
    assert (norm["length_mm"], norm["width_mm"], norm["height_mm"]) == (305, 215, 170)
    assert (norm["inner_length_mm"], norm["inner_width_mm"], norm["inner_height_mm"]) == (305, 215, 170)
    assert norm["material"] == "Kraftliner"
    assert norm["flute_type"] == "cannelure B"
    assert norm["flute_wall"] == "simple cannelure"
    assert norm["color"] == "Brun"
    assert norm["pallet_quantity"] == 1280

    assert len(product.images) == 1
    image = product.images[0]
    assert image["url"].startswith("https://www.antalis.fr/mediashare/g4media/images/LOSQL_MI2_1048_")
    assert set(image["variants"]) == {"LOSQS", "LOSQL"}
    assert product.extra["documents"][0]["url"].endswith("TS_FR_caisse_503623_00_ISS_20261009.pdf")
    assert product.extra["sales_units"]["962340981"]["baseUnitQty"] == 10

    prices = product.prices
    assert prices.price_found
    assert len(prices.tiers) == 1
    tier = prices.tiers[0]
    assert tier.price == Decimal("0.81")
    assert (tier.currency, tier.price_basis, tier.tax_mode) == ("EUR", PriceBasis.UNIT, TaxMode.EXCL_TAX)
    assert tier.min_quantity == Decimal("10") and tier.max_quantity is None
    assert tier.price_unit == "unité" and tier.price_unit_quantity == Decimal(1)
    assert tier.is_promotional is True
    assert tier.metadata["strikethrough_price"] == "1.35"
    assert product.warnings == []


def test_degressive_price_tiers(adapter):
    product = adapter.parse_product(page("sku_586283.html"))
    assert product.catalog_reference == "586283"
    bands = [(t.min_quantity, t.max_quantity, t.price) for t in product.prices.tiers]
    assert bands == [
        (Decimal(36), Decimal(108), Decimal("3.07")),
        (Decimal(108), Decimal(180), Decimal("2.76")),
        (Decimal(180), None, Decimal("2.29")),
    ]
    assert all(
        not t.max_inclusive and t.quantity_unit == "unité" and t.is_promotional is False for t in product.prices.tiers
    )
    assert [t.metadata["tier_label"] for t in product.prices.tiers] == ["1 boite(s)", "3 boite(s)", "5 boite(s)"]
    assert product.normalized_attributes["thickness_um"] == 120
    assert product.normalized_attributes["roll_length_m"] == 50
    assert product.normalized_attributes["core_diameter_mm"] == 76
    assert len(product.images) == 2
    assert product.warnings == []


def test_price_per_thousand_units(adapter):
    product = adapter.parse_product(page("sku_385709_per_1000.html"))
    tiers = product.prices.tiers
    assert [(t.min_quantity, t.max_quantity, t.price) for t in tiers][:2] == [
        (Decimal(2500), Decimal(20000), Decimal("101.47")),
        (Decimal(20000), Decimal(40000), Decimal("90.21")),
    ]
    assert tiers[-1].max_quantity is None
    # "€ 101,47 / 1 000 feuille(s)": the price is for 1000 sheets, not for one
    assert all(t.price_unit == "feuille(s)" and t.price_unit_quantity == Decimal(1000) for t in tiers)
    assert all(t.quantity_unit == "feuille(s)" for t in tiers)


def test_price_only_parse_matches_full_parse(adapter):
    full = adapter.parse_product(page("sku_586283.html"))
    price_page = adapter.parse_prices(page("sku_586283.html"))
    assert price_page.catalog_reference == "586283"
    assert price_page.prices.tiers == full.prices.tiers


def test_product_parser_rejects_listing_pages(adapter):
    with pytest.raises(UnexpectedPageError):
        adapter.parse_product(page("family_hq02953.html"))
    with pytest.raises(UnexpectedPageError):
        adapter.parse_prices(page("plp_hq02947.html"))


@pytest.mark.parametrize(
    ("name", "ld_price"), [("sku_534997_on_request.html", "3999.0"), ("sku_628031_on_request.html", "49.5")]
)
def test_price_on_request_pages(adapter, name, ld_price):
    product = adapter.parse_product(page(name))
    prices = product.prices
    # the empty add-to-cart modal template must not be taken for the product price block
    assert prices.metadata["price_display"] == "on_request"
    assert prices.metadata["page_action"] == "Demander à être rappelé."
    assert [t.price for t in prices.tiers] == [Decimal(ld_price)]
    tier = prices.tiers[0]
    assert (tier.price_basis, tier.tax_mode) == (PriceBasis.UNKNOWN, TaxMode.UNKNOWN)
    assert tier.metadata["displayed"] is False and tier.metadata["tier_source"] == "json_ld_offer"
    assert any("not displayed" in w for w in product.warnings)
    assert product.extra["price_display"] == "on_request"


def test_price_on_request_can_be_recorded_as_no_price(adapter):
    from dataclasses import replace

    strict = type(adapter)(
        replace(adapter.config, options={**adapter.config.options, "record_undisplayed_json_ld_price": False})
    )
    prices = strict.parse_prices(page("sku_628031_on_request.html")).prices
    assert prices.price_found is False and prices.tiers == []
    assert "on request" in prices.reason


def test_missing_price_block_is_not_a_failure(adapter):
    html = fixture_text("antalis/sku_503623.html").replace("data-price-bloc", "data-removed-bloc")
    html = html.replace('"offers":', '"x-offers":')  # no structured-data price either
    product = adapter.parse_product(make_page(html))
    assert product.prices.price_found is False
    assert product.prices.tiers == []
    assert product.prices.reason


def test_missing_sections_are_unknown_not_empty(adapter):
    html = fixture_text("antalis/sku_503623.html")
    html = html.replace('class="sku-attributes"', 'class="x-removed"')
    html = html.replace("eshop-vertical-gallery", "x-gallery").replace('"image":', '"x-image":')
    html = html.replace('id="inpageNavigationItem-03"', 'id="x-docs"')
    product = adapter.parse_product(make_page(html))
    assert product.attributes is None  # never a partial list that would replace a full one
    assert product.normalized_attributes is None
    assert product.images is None
    assert product.extra["documents"] is None
    assert any("characteristics table" in w for w in product.warnings)


def test_unparseable_price_tier_rejects_the_capture(adapter):
    html = fixture_text("antalis/sku_586283.html").replace('data-variant="[108,180]"', 'data-variant="[1.0.8]"')
    with pytest.raises(PageParseError):
        adapter.parse_product(make_page(html))
    with pytest.raises(PageParseError):
        adapter.parse_prices(make_page(html))


def test_listing_announcing_results_without_cards_is_a_parse_error(adapter):
    html = fixture_text("antalis/plp_hq02947.html").replace('class="card"', 'class="x-card"')
    with pytest.raises(PageParseError):
        adapter.parse_listing(make_page(html))
