"""Live smoke tests through Scrape.do. Opt-in: ``uv run pytest -m live --live`` (consumes ~3 credits)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from competitor_catalog.app import build_fetcher
from competitor_catalog.core.adapter import load_adapter
from competitor_catalog.core.config import load_competitor_config
from competitor_catalog.core.types import PageKind
from competitor_catalog.settings import load_settings

pytestmark = pytest.mark.live

SKU_503623 = (
    "https://www.antalis.fr/eshop/packaging-machines-emballage/caisses-carton-palettes/"
    "caisse-carton-a-fond-automatique-hauteur-variable-simple-cannelure-305-x-215-x-170-mm-pdp-hq02953/sku-503623"
)


@pytest.fixture(scope="module")
def live():
    settings = load_settings()
    if not settings.scrapedo_api_key:
        pytest.skip("SCRAPEDO_API_KEY not set")
    config = load_competitor_config(settings.competitor_config_dir, "antalis_fr")
    with build_fetcher(settings, config, interval=1) as client:
        yield client, load_adapter(config)


def test_account_is_active(live):
    client, _ = live
    info = client.account_info()
    assert info["IsActive"] is True
    assert info["RemainingMonthlyRequest"] > 0


def test_root_category_is_reachable_and_parsable(live):
    client, adapter = live
    page = client.fetch("https://www.antalis.fr/eshop/packaging-machines-emballage")
    assert adapter.classify(page) is PageKind.LISTING
    listing = adapter.parse_listing(page)
    assert listing.category.url.endswith("/packaging-machines-emballage/cat-3")
    assert len(listing.child_category_urls) > 10
    assert page.meta["request_cost"] == 1  # no rendering / premium proxy needed


def test_sku_503623_live(live):
    client, adapter = live
    product = adapter.parse_product(client.fetch(SKU_503623))
    assert product.catalog_reference == "503623"
    assert product.category.external_id == "HQ02953"
    assert product.normalized_attributes["length_mm"] == 305
    assert product.prices.price_found and product.prices.tiers[0].price > Decimal(0)
