"""Cenpac adapter tests on saved responses (no network): URLs, scope, classification, listings."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from competitor_catalog.adapters.cenpac.prices import parse_price_tiers
from competitor_catalog.adapters.cenpac.variant_page import _images, data_layer, page_template, read_variant_page
from competitor_catalog.core.adapter import load_adapter
from competitor_catalog.core.config import load_competitor_config
from competitor_catalog.core.errors import PageParseError, UnexpectedPageError
from competitor_catalog.core.html import parse_html
from competitor_catalog.core.types import CategoryRef, PageKind, TaxMode
from competitor_catalog.settings import DEFAULT_COMPETITOR_CONFIG_DIR
from tests.conftest import fixture_text, make_page

API = "https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-"
ROOT = f"{API}/categories/Cenpac"
WEB = "https://www.cenpac.fr"


@pytest.fixture(scope="module")
def adapter():
    return load_adapter(load_competitor_config(DEFAULT_COMPETITOR_CONFIG_DIR, "cenpac_fr"))


def page(name: str, url: str):
    return make_page(fixture_text(f"cenpac/{name}"), url)


def raw(name: str) -> dict:
    return json.loads(fixture_text(f"cenpac/{name}"))


# ---------------------------------------------------------------- URLs
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{ROOT}/", ROOT),
        (
            f"{API}/categories/Cenpac/1000/products?offset=0&amount=200",
            f"{API}/categories/Cenpac/1000/products?amount=200&offset=0",
        ),
        (
            f"{WEB}/caisses-cartons/caisses-demenagement-archivage/caisses-demenagement/"
            "caisse-carton-demenagement-imp-poignees-545-x-345-x-325-mm/p361394",
            f"{WEB}/-/-/p361394",
        ),
        ("HTTPS://WWW.CENPAC.FR/-/gobelet/p642271?x=1#y", f"{WEB}/-/-/p642271"),
        (f"{WEB}/-/-/p06612070", f"{WEB}/-/-/p06612070"),
        (f"{API}/products/11154", f"{API}/products/11154"),
        (f"{WEB}/promotions", f"{WEB}/promotions"),  # a lower-case word is not a SKU
        (f"{WEB}/-/-/pIMP001", f"{WEB}/-/-/pIMP001"),
    ],
)
def test_normalize_url(adapter, url, expected):
    assert adapter.normalize_url(url) == expected


def test_url_builders(adapter):
    assert adapter.product_url("*****") == f"{API}/products/%2A%2A%2A%2A%2A"
    assert adapter.product_url("06703") == f"{API}/products/06703"
    assert adapter.variations_url("11226", 50) == f"{API}/products/11226/variations?amount=200&offset=50"
    assert adapter.variations_url("11154") == f"{API}/products/11154/variations?amount=200"
    assert adapter.variant_url("06612070") == f"{WEB}/-/-/p06612070"
    products = f"{API}/categories/Cenpac/1000/products?amount=200&offset=0"
    assert adapter.category_products_url(["Cenpac", "1000"]) == products


def test_config_root_is_the_canonical_root(adapter):
    assert [adapter.normalize_url(u) for u in adapter.config.root_urls] == [ROOT]


@pytest.mark.parametrize(
    ("category_url", "ancestors", "expected"),
    [
        (ROOT, [], True),
        (f"{ROOT}/1000/1004/1004a", [ROOT, f"{ROOT}/1000", f"{ROOT}/1000/1004"], True),
        (f"{ROOT}/12000", [ROOT], False),
        (f"{API}/products/11154", [ROOT, f"{ROOT}/1000", f"{ROOT}/1000/1004", f"{ROOT}/1000/1004/1004a"], True),
        (
            f"{API}/products/20841",
            [ROOT, f"{ROOT}/12000", f"{ROOT}/12000/12002", f"{ROOT}/12000/12002/12002c"],
            False,
        ),
        (f"{API}/products/IMP001", [f"{API}/categories/NegotiatedPrices"], False),
        (f"{API}/products/X", [ROOT], False),
        (f"{ROOT}/17000/17001", [ROOT, f"{ROOT}/17000"], True),
    ],
)
def test_scope(adapter, category_url, ancestors, expected):
    assert adapter.is_in_scope(category_url, ancestors, set()) is expected


# ---------------------------------------------------------------- classification
BLOCKED_OR_FOREIGN = [
    "",
    "[]",
    "Not Found (Product with SKU 'ZZNOTEXIST99' not found in channel 'Cenpac-Front')",
    "Bad Request (amount must not exceed 200)",
    fixture_text("cenpac/access_denied.html"),
    '{"elements": []}',
]


@pytest.mark.parametrize("body", BLOCKED_OR_FOREIGN)
def test_blocked_or_foreign_responses_are_rejected(adapter, body):
    fetched = make_page(body, f"{API}/products/11154")
    with pytest.raises(UnexpectedPageError):
        adapter.classify(fetched)
    with pytest.raises(UnexpectedPageError):
        adapter.parse_listing(fetched)
    if not body.lstrip().startswith("{"):  # non-JSON bodies: no variant page either
        with pytest.raises(UnexpectedPageError):
            adapter.parse_product(fetched)


def test_classify_api_resources_are_listings(adapter):
    assert adapter.classify(page("category_root.json", ROOT)) is PageKind.LISTING
    assert adapter.classify(page("product_master_11154.json", f"{API}/products/11154")) is PageKind.LISTING
    variations = page("variations_11154.json", f"{API}/products/11154/variations?amount=200")
    assert adapter.classify(variations) is PageKind.LISTING


# ---------------------------------------------------------------- Step 3: listings
def test_root_keeps_only_the_configured_branches(adapter):
    result = adapter.parse_listing(page("category_root.json", ROOT))
    assert result.category.kind == "root"
    assert result.category.ancestors == []
    assert result.category.external_id == "Cenpac"
    assert result.category.name == "Les produits"
    assert result.child_category_urls == [
        f"{ROOT}/{i}" for i in ["17000", "9000", "1000", "2000", "4000", "3000", "6000", "5000", "7000", "20000"]
    ]
    assert not any(u.endswith("/products") for u in result.child_category_urls)


def test_category_identity_and_children(adapter):
    result = adapter.parse_listing(page("category_1000.json", f"{ROOT}/1000"))
    cat = result.category
    assert cat.url == f"{ROOT}/1000"
    assert cat.external_id == "Cenpac/1000"
    assert cat.name == "Caisses et cartons"
    assert [(a.url, a.name, a.external_id) for a in cat.ancestors] == [(ROOT, "Les produits", "Cenpac")]
    assert result.child_category_urls == [
        *[f"{ROOT}/1000/{i}" for i in ["1007", "1006", "1001", "1002", "1005", "1003", "1004"]],
        f"{API}/categories/Cenpac/1000/products?amount=200&offset=0",
    ]


def test_leaf_category(adapter):
    result = adapter.parse_listing(page("category_1000_1004_1004a.json", f"{ROOT}/1000/1004/1004a"))
    assert result.category.external_id == "Cenpac/1000/1004/1004a"
    assert [a.external_id for a in result.category.ancestors] == ["Cenpac", "Cenpac/1000", "Cenpac/1000/1004"]
    assert result.child_category_urls == [f"{API}/categories/Cenpac/1000/1004/1004a/products?amount=200&offset=0"]


def test_small_product_list_is_complete_and_listed_in_its_category(adapter):
    url = f"{ROOT}/1000/1004/1004a/products?amount=200&offset=0"
    result = adapter.parse_listing(page("products_1000_1004_1004a.json", url))
    assert len(result.child_category_urls) == 6
    assert result.child_category_urls[0] == f"{API}/products/18702"
    assert result.child_category_urls[-1] == f"{API}/products/08090"
    assert result.next_page_url is None
    assert result.expected_item_count is None
    assert result.listed_in == f"{ROOT}/1000/1004/1004a"


def test_product_list_pagination(adapter):
    first = adapter.parse_listing(page("products_1000_offset_0.json", f"{ROOT}/1000/products?amount=200&offset=0"))
    assert len(first.child_category_urls) == 50
    assert first.next_page_url == f"{API}/categories/Cenpac/1000/products?amount=200&offset=50"
    assert first.expected_item_count == 67
    assert first.listed_in == f"{ROOT}/1000"

    last = adapter.parse_listing(page("products_1000_offset_50.json", f"{ROOT}/1000/products?amount=200&offset=50"))
    assert len(last.child_category_urls) == 17
    assert last.child_category_urls[0] == f"{API}/products/11403"
    assert last.next_page_url is None

    big = adapter.parse_listing(page("products_9000_amount_200.json", f"{ROOT}/9000/products?amount=200&offset=0"))
    assert len(big.child_category_urls) == 200
    assert big.next_page_url == f"{API}/categories/Cenpac/9000/products?amount=200&offset=200"
    assert big.expected_item_count == 413


def test_merchandising_sub_category_list(adapter):
    url = f"{ROOT}/1000/1006/1006a/products?amount=200&offset=0"
    result = adapter.parse_listing(page("products_1000_1006_1006a.json", url))
    assert len(result.child_category_urls) == 25
    assert result.listed_in == f"{ROOT}/1000/1006/1006a"
    assert result.child_category_urls[0] == f"{API}/products/11226"


def test_family_in_scope(adapter):
    cat = adapter.parse_listing(page("product_master_11154.json", f"{API}/products/11154")).category
    assert cat.kind == "product_family"
    assert cat.external_id == "11154"
    assert cat.name == "Caisse carton déménagement"
    assert [a.url for a in cat.ancestors] == [ROOT, f"{ROOT}/1000", f"{ROOT}/1000/1004", f"{ROOT}/1000/1004/1004a"]
    result = adapter.parse_listing(page("product_master_11154.json", f"{API}/products/11154"))
    assert result.next_page_url == f"{API}/products/11154/variations?amount=200"
    assert adapter.is_in_scope(cat.url, [a.url for a in cat.ancestors], set())


@pytest.mark.parametrize(
    ("fixture", "sku"),
    [
        ("product_master_20841.json", "20841"),
        ("product_master_08065.json", "08065"),
        ("product_master_MA001.json", "MA001"),
        ("product_master_stars.json", "*****"),
    ],
)
def test_out_of_scope_families(adapter, fixture, sku):
    url = adapter.product_url(sku)
    cat = adapter.parse_listing(page(fixture, url)).category
    assert adapter.is_in_scope(cat.url, [a.url for a in cat.ancestors], set()) is False


def test_stars_family_urls(adapter):
    result = adapter.parse_listing(page("product_master_stars.json", f"{API}/products/%2A%2A%2A%2A%2A"))
    assert result.category.url == f"{API}/products/%2A%2A%2A%2A%2A"
    assert result.next_page_url == f"{API}/products/%2A%2A%2A%2A%2A/variations?amount=200"


def test_leading_zero_family(adapter):
    cat = adapter.parse_listing(page("product_master_06703.json", f"{API}/products/06703")).category
    assert cat.external_id == "06703"
    assert cat.ancestors[-1].name == "Accessoires de palettisation"
    assert cat.ancestors[-1].external_id == "Cenpac/3000/3003/3003c"
    assert adapter.is_in_scope(cat.url, [a.url for a in cat.ancestors], set())


def test_non_master_product_is_ignored(adapter):
    url = f"{API}/products/catalogue_cenpac"
    fetched = page("product_catalogue_cenpac.json", url)
    assert adapter.classify(fetched) is PageKind.LISTING
    result = adapter.parse_listing(fetched)
    assert result.category is None
    assert any("not a variation master" in w for w in result.warnings)
    assert result.child_category_urls == []
    assert result.product_links == []


def test_variations(adapter):
    url = f"{API}/products/11154/variations?amount=200"
    result = adapter.parse_listing(page("variations_11154.json", url))
    assert [(link.url, link.reference) for link in result.product_links] == [
        (f"{WEB}/-/-/p{s}", s) for s in ["361394", "516476", "605103", "605104"]
    ]
    assert result.next_page_url is None

    first = adapter.parse_listing(page("variations_11226_offset_0.json", f"{API}/products/11226/variations?amount=200"))
    assert len(first.product_links) == 50
    assert first.next_page_url == f"{API}/products/11226/variations?amount=200&offset=50"
    assert first.expected_item_count == 189

    second = adapter.parse_listing(
        page("variations_11226_offset_50.json", f"{API}/products/11226/variations?amount=200&offset=50")
    )
    assert second.next_page_url == f"{API}/products/11226/variations?amount=200&offset=100"

    whole_url = f"{API}/products/11226/variations?amount=200"
    whole = adapter.parse_listing(page("variations_11226_amount_200.json", whole_url))
    assert len(whole.product_links) == 189
    assert whole.next_page_url is None

    empty = adapter.parse_listing(
        page("variations_12605_offset_50.json", f"{API}/products/12605/variations?amount=200&offset=50")
    )
    assert empty.product_links == []
    assert empty.next_page_url is None

    single = adapter.parse_listing(page("variations_06703.json", f"{API}/products/06703/variations?amount=200"))
    assert [link.url for link in single.product_links] == [f"{WEB}/-/-/p639405"]


# ---------------------------------------------------------------- Step 4: variant page
def vp(name: str, sku: str):
    return read_variant_page(fixture_text(f"cenpac/{name}"), f"{WEB}/-/-/p{sku}")


def test_variant_page_box():
    v = vp("variant_361394.html", "361394")
    assert v.sku == "361394"
    assert v.title == "Caisse carton déménagement - SANS IMP. AVEC POIGNÉES 545 x 345 x 325 mm"
    assert v.description == "Pour transporter facilement vos produits !"
    assert len(v.attributes) == 14
    assert v.attributes[0] == ("Hauteur extérieure (mm)", "340")
    assert ("Dimensions intérieures Lxlxh (mm)", "545 x 345 x 325") in v.attributes
    assert ("Paquet de", "20") in v.attributes
    assert v.long_description.startswith(
        "Caisse carton en simple cannelure résistante.\nFabrication en carton 100% recyclé."
    )
    assert len(v.images) == 1
    assert v.images[0]["name"] == "11154_361394_caisse_demenagement_sit"
    assert v.images[0]["url"] == (
        "https://images.cenpac.fr/is/image/Cenpac/products/Caisse-carton-demenagement.jpg"
        "?template=template-cenpac-std&$image=Cenpac/11154_361394_caisse_demenagement_sit&$img-base$"
    )
    assert v.images[0]["thumbnail_url"].endswith("&$img-miniature$")
    assert v.master_sku == "11154"
    assert v.family_name == "Caisse carton déménagement"
    assert v.category_path == [
        ("1000", "Caisses et cartons"),
        ("1004", "Caisses déménagement et archivage"),
        ("1004a", "Caisses de déménagement"),
    ]


def test_variant_page_leading_zero():
    v = vp("variant_06612070.html", "06612070")
    assert v.sku == "06612070"
    assert v.master_sku == "06601"
    assert ("Epaisseur (µ)", "15") in v.attributes
    assert ("Longueur (m)", "300") in v.attributes


def test_variant_page_customer_variant():
    v = vp("variant_605103_no_price.html", "605103")
    assert v.title == "CAISSE AMERIC 1.C INTERMARCHE"
    assert v.attributes == []
    assert v.master_sku == "11154"
    assert v.category_path == [("13000", "Produits Divers"), ("13010", "Divers")]


def test_variant_page_without_category_or_description():
    v = vp("variant_IMP001_no_price.html", "IMP001")
    assert v.sku == "IMP001"
    assert v.master_sku == "MA001"
    assert v.category_path == []
    assert v.long_description is None
    assert v.description == ""


def test_variant_page_images_deduplicated():
    v = vp("variant_643415_promo.html", "643415")
    assert [img["name"] for img in v.images] == [
        "34348_art_plaque_carton_ondule_1_prod",
        "34348_643415_plaque_carton_ondule_2_sit",
        "34348_643415_plaque_carton_ondule_1_sit",
    ]


def test_access_denied_page_is_rejected():
    with pytest.raises(UnexpectedPageError):
        vp("access_denied.html", "174")


def test_page_template():
    assert page_template(fixture_text("cenpac/variant_361394.html")) == "Produit_sku"
    assert page_template(fixture_text("cenpac/access_denied.html")) is None


# ---------------------------------------------------------------- Step 5: prices
def T(lo, hi, price):
    return (Decimal(lo), None if hi is None else Decimal(hi), Decimal(price))


def prices(name: str, sku: str):
    return parse_price_tiers(parse_html(fixture_text(f"cenpac/{name}")), sku, "EUR")


def bounds(snapshot):
    return [(t.min_quantity, t.max_quantity, t.price) for t in snapshot.tiers]


def test_box_tiers():
    s = prices("variant_361394.html", "361394")
    assert bounds(s) == [T(20, 100, "3.21"), T(100, 200, "2.73"), T(200, None, "2.57")]
    for t in s.tiers:
        assert t.price_unit == t.quantity_unit == "caisse"
        assert t.price_unit_quantity == Decimal(1)
        assert t.tax_mode == TaxMode.EXCL_TAX
        assert t.currency == "EUR"
        assert t.is_promotional is False
    assert s.tiers[1].metadata["discount"] == "-15%"
    assert s.metadata["order_step"] == 20
    assert s.metadata["min_order_quantity"] == 20
    assert s.metadata["sales_unit_label"] == "la caisse"


def test_colis_de_1000():
    s = prices("variant_642271_colis_de_1000.html", "642271")
    assert bounds(s) == [T(1, 3, "40.85"), T(3, 5, "38.81"), T(5, None, "36.77")]
    assert s.tiers[0].price_unit == s.tiers[0].quantity_unit == "colis"
    assert s.tiers[0].price_unit_quantity == Decimal(1)
    assert s.tiers[0].metadata["units_per_price_unit"] == 1000
    assert s.tiers[0].metadata["site_quantity_unit"] == "colis"
    assert s.metadata["order_step"] == 1


def test_promo_struck_price():
    s = prices("variant_643415_promo.html", "643415")
    assert bounds(s) == [T(1, None, "7.80")]
    assert s.tiers[0].is_promotional is True
    assert s.tiers[0].metadata["struck_price"] == "24.94"
    assert s.tiers[0].metadata["discount"] == "-75%"
    assert s.tiers[0].metadata["units_per_price_unit"] == 50

    film = prices("variant_639405.html", "639405")
    assert bounds(film) == [T(1, None, "149.82")]
    assert film.tiers[0].is_promotional is True
    assert film.tiers[0].metadata["struck_price"] == "205.87"
    assert film.tiers[0].price_unit == "rouleau"


def test_moq_above_first_tier():
    s = prices("variant_11226001_moq.html", "11226001")
    assert bounds(s) == [
        T(10, 100, "5.16"),
        T(100, 200, "4.64"),
        T(200, 400, "4.39"),
        T(400, 800, "4.13"),
        T(800, None, "3.87"),
    ]
    assert s.tiers[0].metadata["site_quantity"] == "1"


def test_tiers_counted_in_colis_are_converted():
    s = prices("variant_636554_tiers_by_colis.html", "636554")
    assert bounds(s) == [T(36, 108, "4.30"), T(108, 180, "3.87"), T(180, None, "3.66")]
    assert s.tiers[0].price_unit == s.tiers[0].quantity_unit == "rouleau"
    assert [t.metadata["site_quantity"] for t in s.tiers] == ["1", "3", "5"]
    assert s.metadata["tiers_by_colis"] is True


def test_film_roll():
    s = prices("variant_06612070.html", "06612070")
    assert bounds(s) == [T(6, 12, "17.37"), T(12, 24, "14.76"), T(24, 48, "13.90"), T(48, None, "13.03")]
    assert s.tiers[0].price_unit == "bobine"


def test_unpriced_variant_is_not_a_failure():
    s = prices("variant_605103_no_price.html", "605103")
    assert s.price_found is False
    assert s.tiers == []
    assert s.reason == "Prix non disponible, contactez nous."
    assert s.metadata["order_step"] == 20


def test_no_price_marker_wins_over_listed_tiers():
    assert prices("variant_IMP001_no_price.html", "IMP001").price_found is False


def test_missing_price_config_is_a_parse_error():
    with pytest.raises(PageParseError):
        prices("variant_361394.html", "999")


def price_page(
    priceslist: list[dict],
    *,
    qty="20",
    sold_by: str | None = "vendu par colis de 20",
    unit: str | None = "la caisse",
    headline: str | None = "2,57",
    ht: bool = True,
    by_colis: bool = False,
    offers: bool = True,
):
    """Minimal variant page with one price block for sku 1, same markup as variant_361394.html."""
    config = json.dumps({"sku": "1", "qty": qty, "priceslist": priceslist})
    sold = f"<span>{sold_by}</span>" if sold_by is not None else ""
    unit_html = f'<span class="font-semibold whitespace-no-wrap">{unit}</span>' if unit is not None else ""
    head = f'<span itemprop="price" content="{headline}">{headline}</span>' if headline is not None else ""
    ht_html = '<span class="font-semibold">HT</span>' if ht else ""
    block = f'<div itemprop="offers"><div>{head}{ht_html} {unit_html}</div>{sold}</div>' if offers else ""
    hbs = '<script id="HBS_degressiveprices">{{ this.qty }} colis et +</script>' if by_colis else ""
    return parse_html(
        f"<html><body>{block}"
        f"<div data-degressiveprices=\"1\" data-degressiveprices-config='{config}'></div>"
        f"{hbs}</body></html>"
    )


def price_snapshot(priceslist: list[dict], **kwargs):
    return parse_price_tiers(price_page(priceslist, **kwargs), "1", "EUR")


def test_price_page_helper_reads_a_plain_block():
    s = price_snapshot([{"qty": "20", "price": "3,21 €"}, {"qty": "100", "price": "2,73 €"}])
    assert bounds(s) == [T(20, 100, "3.21"), T(100, None, "2.73")]
    assert s.metadata["sales_unit"] == "caisse"


def test_offers_block_missing_is_a_parse_error():
    with pytest.raises(PageParseError):
        price_snapshot([{"qty": "20", "price": "3,21"}], offers=False)


def test_empty_price_list_is_not_a_failure():
    s = price_snapshot([])
    assert s.price_found is False
    assert s.reason == "no price tiers on the page"


def test_sales_unit_missing_is_a_parse_error():
    with pytest.raises(PageParseError):
        price_snapshot([{"qty": "20", "price": "3,21"}], unit=None)


def test_tax_mode_not_shown_is_a_warning():
    s = price_snapshot([{"qty": "20", "price": "3,21"}], ht=False)
    assert s.tiers[0].tax_mode == TaxMode.UNKNOWN
    assert "tax mode not shown" in s.warnings


def test_tiers_counted_in_colis_without_a_colis_size():
    s = price_snapshot([{"qty": "1", "price": "40,85"}], sold_by=None, by_colis=True)
    assert s.tiers[0].quantity_unit == "colis"
    assert s.tiers[0].metadata["site_quantity_unit"] == "colis"
    assert "tiers counted in colis, colis size unknown" in s.warnings


def test_zero_price_tier_is_dropped_with_a_warning():
    s = price_snapshot([{"qty": "20", "price": "0"}, {"qty": "100", "price": "2,73"}])
    assert bounds(s) == [T(100, None, "2.73")]
    assert "zero price tier dropped (qty 20)" in s.warnings


def test_only_zero_price_tiers_means_no_price():
    s = price_snapshot([{"qty": "20", "price": "0"}])
    assert s.price_found is False
    assert s.reason == "only zero-price tiers"


def test_duplicate_tier_at_the_same_quantity_is_dropped():
    s = price_snapshot(
        [{"qty": "20", "price": "3,21"}, {"qty": "20", "price": "3,00"}, {"qty": "100", "price": "2,73"}]
    )
    assert bounds(s) == [T(20, 100, "3.21"), T(100, None, "2.73")]
    assert "duplicate price tier at 20 dropped" in s.warnings


def test_minimum_order_above_the_first_band_is_a_warning():
    s = price_snapshot([{"qty": "20", "price": "3,00"}, {"qty": "30", "price": "2,00"}], qty="50")
    assert s.tiers[0].min_quantity == Decimal(20)
    assert "minimum order quantity 50 above the first tier band" in s.warnings


def test_headline_price_not_the_lowest_tier_is_a_warning():
    s = price_snapshot([{"qty": "20", "price": "3,21"}, {"qty": "100", "price": "2,73"}], headline="9,99")
    assert "headline price 9.99 is not the lowest tier price" in s.warnings
    assert s.metadata["headline_price"] == "9.99"


def test_sales_unit_starting_with_les():
    s = price_snapshot([{"qty": "20", "price": "3,21"}], unit="les caisses")
    assert s.metadata["sales_unit"] == "caisses"
    assert s.metadata["sales_unit_label"] == "les caisses"


def test_unit_order_step_for_a_piece():
    s = price_snapshot([{"qty": "1", "price": "0,81"}], sold_by="vendu à l'unité", qty="1")
    assert s.metadata["order_step"] == 1


def test_json_numbers_in_the_price_config_are_read():
    s = price_snapshot([{"qty": 20, "price": 3.21}, {"qty": 100, "price": 2.73}], qty=20)
    assert bounds(s) == [T(20, 100, "3.21"), T(100, None, "2.73")]
    assert s.metadata["min_order_quantity"] == 20


def test_unreadable_struck_price_is_a_warning():
    s = price_snapshot([{"qty": "20", "price": "3,21", "oldprice": "n.c."}])
    assert s.tiers[0].is_promotional is False
    assert s.tiers[0].metadata["struck_price"] is None
    assert any("unparseable struck price 'n.c.'" in w for w in s.warnings)


# ---------------------------------------------------------------- Step 6: parse_product / parse_prices
def test_variant_product(adapter):
    fetched = page("variant_361394.html", f"{WEB}/-/-/p361394")
    product = adapter.parse_product(fetched)
    assert product.url == f"{WEB}/-/-/p361394"  # the variant URL, not the family canonical link
    assert product.catalog_reference == product.external_id == "361394"
    assert product.category == CategoryRef(f"{API}/products/11154", "Caisse carton déménagement", "11154")
    assert [b.external_id for b in product.breadcrumb] == [
        "Cenpac",
        "Cenpac/1000",
        "Cenpac/1000/1004",
        "Cenpac/1000/1004/1004a",
        "11154",
    ]
    assert product.normalized_attributes == {
        "length_mm": 553,
        "width_mm": 353,
        "height_mm": 340,
        "material": "CARTON",
        "color": "ÉCRU",
        "quality_code": "1/C",
        "print": "NEUTRE",
        "bundle_quantity": 20,
        "pallet_quantity": 440,
        "inner_length_mm": 545,
        "inner_width_mm": 345,
        "inner_height_mm": 325,
    }
    assert product.extra["order_step"] == 20
    assert product.extra["sales_unit"] == "caisse"
    assert adapter.classify(fetched) is PageKind.PRODUCT


def test_roll_normalization(adapter):
    product = adapter.parse_product(page("variant_06612070.html", f"{WEB}/-/-/p06612070"))
    assert product.normalized_attributes["width_mm"] == 450
    assert product.normalized_attributes["roll_length_m"] == 300
    assert product.normalized_attributes["thickness_um"] == 15
    assert product.normalized_attributes["bundle_quantity"] == 6


def test_customer_variant_breadcrumb_stays_in_scope(adapter):
    product = adapter.parse_product(page("variant_605103_no_price.html", f"{WEB}/-/-/p605103"))
    assert product.prices.price_found is False
    assert [b.external_id for b in product.breadcrumb] == ["Cenpac", "11154"]
    assert any("outside the crawl scope" in w for w in product.warnings)
    assert product.extra["page_category_ids"] == ["13000", "13010"]


VARIANT_FIXTURES = [
    ("variant_361394.html", "361394"),
    ("variant_642271_colis_de_1000.html", "642271"),
    ("variant_643415_promo.html", "643415"),
    ("variant_605103_no_price.html", "605103"),
    ("variant_11226001_moq.html", "11226001"),
    ("variant_636554_tiers_by_colis.html", "636554"),
    ("variant_06612070.html", "06612070"),
    ("variant_639405.html", "639405"),
    ("variant_IMP001_no_price.html", "IMP001"),
]


@pytest.mark.parametrize(("name", "sku"), VARIANT_FIXTURES)
def test_price_refresh_identity(adapter, name, sku):
    fetched = page(name, f"{WEB}/-/-/p{sku}")
    refreshed = adapter.parse_prices(fetched)
    product = adapter.parse_product(fetched)
    assert refreshed.catalog_reference == product.catalog_reference
    assert refreshed.url == product.url
    assert refreshed.prices.tiers == product.prices.tiers


def test_product_parsers_reject_api_json(adapter):
    fetched = page("product_master_11154.json", f"{API}/products/11154")
    with pytest.raises(UnexpectedPageError):
        adapter.parse_product(fetched)
    with pytest.raises(UnexpectedPageError):
        adapter.parse_prices(fetched)
    with pytest.raises(UnexpectedPageError):
        adapter.parse_listing(page("variant_361394.html", f"{WEB}/-/-/p361394"))


# ---------------------------------------------------------------- malformed or unusual API responses
def test_unknown_api_resource_type_is_rejected(adapter):
    with pytest.raises(UnexpectedPageError):
        adapter.classify(make_page('{"type": "Foo"}', f"{API}/products/11154"))


def test_category_without_category_path_is_a_parse_error(adapter):
    body = json.dumps({"type": "Category", "name": "X"})
    with pytest.raises(PageParseError):
        adapter.parse_listing(make_page(body, f"{API}/categories/Cenpac/1000"))


def test_category_items_without_id_are_parse_errors(adapter):
    url = f"{API}/categories/Cenpac/1000"
    no_id_in_path = {"type": "Category", "categoryPath": [{"name": "Root"}]}
    with pytest.raises(PageParseError):
        adapter.parse_listing(make_page(json.dumps(no_id_in_path), url))
    no_id_in_children = {
        "type": "Category",
        "categoryPath": [{"id": "Cenpac", "name": "Root"}],
        "subCategories": [{"name": "child"}],
    }
    with pytest.raises(PageParseError):
        adapter.parse_listing(make_page(json.dumps(no_id_in_children), url))


def test_variation_master_without_sku_is_rejected(adapter):
    body = json.dumps({"type": "Product", "productTypes": ["VARIATION_MASTER"]})
    with pytest.raises(UnexpectedPageError):
        adapter.parse_listing(make_page(body, f"{API}/products/11154"))


def test_variation_master_without_variations_link(adapter):
    body = json.dumps({"type": "Product", "sku": "11154", "name": "Caisse", "productTypes": ["VARIATION_MASTER"]})
    result = adapter.parse_listing(make_page(body, f"{API}/products/11154"))
    assert result.category.external_id == "11154"
    assert result.next_page_url is None
    assert result.warnings == ["variation master without a variations link"]


def test_variations_url_without_a_product_is_rejected(adapter):
    for url in (f"{API}/products/11154?amount=200", f"{WEB}/variations?amount=200"):
        with pytest.raises(UnexpectedPageError):
            adapter.parse_listing(page("variations_11154.json", url))


def test_product_list_outside_a_category_is_rejected(adapter):
    body = json.dumps({"type": "ResourceCollection", "elements": []})
    with pytest.raises(UnexpectedPageError):
        adapter.parse_listing(make_page(body, f"{API}/products?amount=200"))


# ---------------------------------------------------------------- variant page edge cases
def test_images_without_a_name_are_told_apart_by_url():
    gallery = parse_html(
        '<div data-gallery-name="product">'
        '<img data-type="base" src="https://img.example/a.jpg">'
        '<img data-type="base" src="https://img.example/b.jpg">'
        '<img data-type="base" data-name="same" src="https://img.example/c.jpg">'
        '<img data-type="base" data-name="same" src="https://img.example/d.jpg">'
        "</div>"
    )
    assert [img["url"] for img in _images(gallery)] == [
        "https://img.example/a.jpg",
        "https://img.example/b.jpg",
        "https://img.example/c.jpg",
    ]


def test_data_layer_with_a_closing_sequence_inside_a_string():
    text = 'var dataLayer = [gtmHelper.getData({"page": {"env_template": "Produit_sku", "label": "a})]b"}})];'
    assert page_template(text) == "Produit_sku"
    assert data_layer(text)["page"]["label"] == "a})]b"
