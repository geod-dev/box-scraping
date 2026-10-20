"""Generic engine + persistence tests against PostgreSQL (skipped if no test DB).

They use the toy JSON competitor (tests/toy_adapter.py) so they exercise the engine
independently from any website, plus an end-to-end Antalis run on saved fixtures.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from competitor_catalog.core.adapter import load_adapter
from competitor_catalog.core.config import load_competitor_config, parse_competitor_config
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import Category, Product, ScrapeObservation, ScrapeRun, ScrapeRunItem
from competitor_catalog.db.session import transaction
from competitor_catalog.services.catalog_sync import CatalogSync, CatalogSyncOptions
from competitor_catalog.services.price_refresh import PriceRefresh, PriceRefreshOptions
from competitor_catalog.services.runs import RunObserver
from competitor_catalog.settings import DEFAULT_COMPETITOR_CONFIG_DIR
from tests.conftest import FakeFetcher, fixture_text
from tests.toy_adapter import BASE, toy_site

pytestmark = pytest.mark.db

TOY_CONFIG = parse_competitor_config(
    {
        "key": "toy",
        "name": "Toy competitor",
        "base_url": BASE,
        "adapter": "tests.toy_adapter:ToyAdapter",
        "root_urls": [f"{BASE}/root"],
        "request_interval_seconds": 0,
    }
)


@pytest.fixture
def toy(factory):
    with transaction(factory) as session:
        competitor_id = repo.upsert_competitor(session, TOY_CONFIG).id
    adapter = load_adapter(TOY_CONFIG)

    class Env:
        def sync(self, site, **options):
            fetcher = FakeFetcher(site, failing=options.pop("failing", None))
            result = CatalogSync(adapter, fetcher, factory, competitor_id).run(CatalogSyncOptions(**options))
            return result, fetcher

        def refresh(self, site, **options):
            fetcher = FakeFetcher(site, failing=options.pop("failing", None))
            result = PriceRefresh(adapter, fetcher, factory, competitor_id).run(PriceRefreshOptions(**options))
            return result, fetcher

        def products(self):
            with transaction(factory) as s:
                return {
                    p.catalog_reference: p
                    for p in s.scalars(select(Product).where(Product.competitor_id == competitor_id))
                }

        def observations(self, ref):
            with transaction(factory) as s:
                pid = s.scalar(select(Product.id).where(Product.catalog_reference == ref))
                return list(
                    s.scalars(
                        select(ScrapeObservation)
                        .where(ScrapeObservation.product_id == pid)
                        .order_by(ScrapeObservation.id)
                    )
                )

        def count(self, model):
            with transaction(factory) as s:
                return s.scalar(select(func.count()).select_from(model))

    env = Env()
    env.factory = factory
    env.adapter = adapter
    env.competitor_id = competitor_id
    env.competitor_id = competitor_id
    return env


def _set(site, url, **changes):
    page = json.loads(site[url])
    page.update(changes)
    site[url] = json.dumps(page)


# --------------------------------------------------------------------------- catalog discovery


def test_discovery_builds_hierarchy_without_duplicates(toy):
    result, fetcher = toy.sync(toy_site())
    assert result.status == "succeeded", result.incomplete_reasons
    assert len(fetcher.requests) == len(set(fetcher.requests))  # every URL fetched once
    assert sorted(result.category_urls) == [f"{BASE}/cat-a", f"{BASE}/cat-b", f"{BASE}/root"]
    assert len(result.product_urls) == len(set(result.product_urls)) == 4

    with transaction(toy.factory) as s:
        cats = {c.name: c for c in s.scalars(select(Category))}
        assert set(cats) == {"Root", "A", "B"}  # the pagination page is not a category
        assert cats["A"].parent_id == cats["Root"].id and cats["B"].parent_id == cats["Root"].id
        assert cats["Root"].parent_id is None
        assert cats["A"].external_id == "CA" and cats["Root"].external_id is None
        products = toy.products()
        assert {r: products[r].category_id for r in products} == {
            "A1": cats["A"].id,
            "A2": cats["A"].id,
            "A3": cats["A"].id,
            "B1": cats["B"].id,
        }
    # initial product scrape also records prices through the shared price persistence
    assert [o.price for o in toy.observations("A2")] == [Decimal("20")]
    assert result.counters["observations_created"] == 4


def test_skip_products_run_persists_discovered_product_urls(toy):
    site = toy_site()
    _set(site, f"{BASE}/cat-b", products=[{"url": f"{BASE}/p/B1", "ref": "B1"}, {"url": f"{BASE}/p/A1", "ref": "A1"}])
    result, fetcher = toy.sync(site, skip_products=True)
    assert result.status == "succeeded"
    assert not any("/p/" in url for url in fetcher.requests)
    assert toy.count(Product) == 0
    with transaction(toy.factory) as s:
        run = s.get(ScrapeRun, result.run_id)
        assert run.full_scope is False
        assert run.stats["product_links_discovered"] == 4
        assert run.stats["duplicate_product_links_ignored"] == 1  # A1 linked from two listings
        urls = s.scalars(
            select(ScrapeRunItem.url).where(ScrapeRunItem.scrape_run_id == run.id, ScrapeRunItem.item_type == "product")
        ).all()
        assert sorted(urls) == sorted(f"{BASE}/p/{r}" for r in ["A1", "A2", "A3", "B1"])


def test_rerun_updates_known_products_and_adds_new_ones(toy):
    site = toy_site()
    toy.sync(site)
    ids_before = {r: p.id for r, p in toy.products().items()}

    site = toy_site(
        {
            "A1": {"price": 11, "category": "A", "title": "Renamed A1"},
            "A2": {"price": 20, "category": "A"},
            "A3": {"price": 30, "category": "A"},
            "A4": {"price": 40, "category": "A"},  # new product
            "B1": {"price": 5, "category": "B"},
        }
    )
    result, _ = toy.sync(site)
    assert result.status == "succeeded"
    assert (result.counters["products_created"], result.counters["products_updated"]) == (1, 4)

    products = toy.products()
    assert len(products) == 5 and toy.count(Product) == 5
    assert all(products[r].id == ids_before[r] for r in ids_before)  # same rows, no duplicates
    assert products["A1"].title == "Renamed A1"
    assert toy.count(Category) == 3
    # price history is appended, never overwritten
    assert [o.price for o in toy.observations("A1")] == [Decimal("10"), Decimal("11")]


def test_failed_listing_page_makes_run_partial_and_marks_nothing_missing(toy):
    toy.sync(toy_site())
    smaller = toy_site({"A1": {"price": 10, "category": "A"}})
    result, _ = toy.sync(smaller, failing={f"{BASE}/cat-b"}, allow_mass_missing=True)
    assert result.status == "partial"
    assert result.counters["products_marked_missing"] == 0
    assert all(p.status == "active" for p in toy.products().values())
    with transaction(toy.factory) as s:
        run = s.get(ScrapeRun, result.run_id)
        failed = s.scalars(
            select(ScrapeRunItem).where(ScrapeRunItem.scrape_run_id == run.id, ScrapeRunItem.status == "failed")
        ).all()
        assert [i.url for i in failed] == [f"{BASE}/cat-b"]
        assert failed[0].error_type == "TransientFetchError"


def test_complete_run_marks_unseen_products_missing_and_revives_them(toy):
    toy.sync(toy_site())
    without_b1 = toy_site(
        {
            r: p
            for r, p in {
                "A1": {"price": 10, "category": "A"},
                "A2": {"price": 20, "category": "A"},
                "A3": {"price": 30, "category": "A"},
            }.items()
        }
    )
    result, _ = toy.sync(without_b1, max_missing_ratio=0.5)
    assert result.status == "succeeded"
    products = toy.products()
    assert products["B1"].status == "missing" and products["B1"].missing_since is not None
    assert products["A1"].status == "active"

    result, _ = toy.sync(toy_site())
    products = toy.products()
    assert products["B1"].status == "active" and products["B1"].missing_since is None
    assert toy.count(Product) == 4


def test_mass_disappearance_is_not_marked_without_confirmation(toy):
    toy.sync(toy_site())
    result, _ = toy.sync(toy_site({"A1": {"price": 10, "category": "A"}}))
    assert result.status == "succeeded"
    assert result.counters["products_marked_missing"] == 0
    with transaction(toy.factory) as s:
        assert "safety threshold" in s.get(ScrapeRun, result.run_id).stats["missing_marking_skipped"]


def test_partial_scope_runs_never_mark_missing(toy):
    toy.sync(toy_site())
    result, _ = toy.sync(toy_site(), max_products=1)
    assert result.status == "partial"
    assert all(p.status == "active" for p in toy.products().values())


def test_incomplete_pagination_is_detected(toy):
    site = toy_site()
    _set(site, f"{BASE}/cat-a?page=2", next=None, expected=3)
    _set(site, f"{BASE}/cat-a", expected=5)  # site announces 5 items, only 3 reachable
    result, _ = toy.sync(site)
    assert result.status == "partial"
    assert any("announces 5 items, 3 collected" in r for r in result.incomplete_reasons)


def test_missing_product_page_is_recorded(toy):
    site = toy_site()
    del site[f"{BASE}/p/A3"]
    result, _ = toy.sync(site)
    assert result.status == "partial"
    with transaction(toy.factory) as s:
        item = s.scalar(select(ScrapeRunItem).where(ScrapeRunItem.url == f"{BASE}/p/A3"))
        assert (item.status, item.http_status) == ("not_found", 404)


def test_missing_vs_empty_values_are_preserved(toy):
    def attributes_sql(ref):
        with transaction(toy.factory) as s:
            return s.execute(
                text("SELECT attributes IS NULL, attributes::text FROM products WHERE catalog_reference = :r"),
                {"r": ref},
            ).one()

    toy.sync(toy_site({"A1": {"price": 10, "category": "A", "attributes": None}}))
    assert attributes_sql("A1") == (True, None)  # not observed -> SQL NULL, not JSON 'null'
    toy.sync(toy_site({"A1": {"price": 10, "category": "A", "attributes": []}}))
    assert attributes_sql("A1") == (False, "[]")  # confirmed empty
    toy.sync(toy_site({"A1": {"price": 10, "category": "A", "attributes": None}}))
    assert attributes_sql("A1") == (False, "[]")  # a later "not observed" never erases knowledge


def test_listing_404_makes_run_partial(toy):
    toy.sync(toy_site())
    site = toy_site()
    del site[f"{BASE}/cat-b"]
    result, _ = toy.sync(site, allow_mass_missing=True)
    assert result.status == "partial"
    assert toy.products()["B1"].status == "active"


def test_category_without_navigation_path_is_a_failure_not_out_of_scope(toy):
    site = toy_site()
    _set(site, f"{BASE}/cat-b", category={"url": f"{BASE}/cat-b", "name": "B", "id": "CB"})
    result, _ = toy.sync(site)
    assert result.status == "partial"
    with transaction(toy.factory) as s:
        item = s.scalar(select(ScrapeRunItem).where(ScrapeRunItem.url == f"{BASE}/cat-b"))
        assert (item.status, item.error_type) == ("failed", "PageParseError")


def test_unexpected_error_on_one_product_does_not_stop_the_run(toy):
    site = toy_site()
    _set(site, f"{BASE}/p/A2", tiers=[{"price": "not-a-number", "min": 1}])  # Decimal() raises
    result, _ = toy.sync(site)
    assert result.status == "partial"
    assert set(toy.products()) == {"A1", "A3", "B1"}
    with transaction(toy.factory) as s:
        item = s.scalar(select(ScrapeRunItem).where(ScrapeRunItem.url == f"{BASE}/p/A2"))
        assert item.status == "failed" and item.error_type == "InvalidOperation"


def test_retry_failed_from_a_catalog_run_matches_by_url(toy):
    site = toy_site()
    toy.sync(site)
    failed, _ = toy.sync(site, failing={f"{BASE}/p/A3"})
    assert failed.status == "partial"
    _, fetcher = toy.refresh(site, retry_failed_run_id=failed.run_id)
    assert fetcher.requests == [f"{BASE}/p/A3"]


def test_catalog_retry_refetches_only_the_failed_pages(toy):
    site = toy_site()
    # first sync: a listing page and a product page fail; B1 (under cat-b) and A3 are never created
    failed, _ = toy.sync(site, failing={f"{BASE}/cat-b", f"{BASE}/p/A3"})
    assert failed.status == "partial" and set(toy.products()) == {"A1", "A2"}
    retry, fetcher = toy.sync(site, retry_failed_run_id=failed.run_id, resume_within_hours=48)
    assert retry.status == "succeeded"
    assert sorted(fetcher.requests) == sorted([f"{BASE}/cat-b", f"{BASE}/p/B1", f"{BASE}/p/A3"])
    assert set(toy.products()) == {"A1", "A2", "A3", "B1"}
    with transaction(toy.factory) as s:
        run = s.get(ScrapeRun, retry.run_id)
        assert run.full_scope is False and run.params["retry_failed_run_id"] == failed.run_id
        assert s.scalar(select(func.count()).select_from(Product).where(Product.status == "missing")) == 0
    # a listing page failing again is a failed page, not a fatal error
    again, _ = toy.sync(site, retry_failed_run_id=failed.run_id, failing={f"{BASE}/cat-b"})
    assert again.status == "partial"


def test_resume_skips_recently_synced_products_but_counts_them_as_seen(toy):
    site = toy_site()
    first, _ = toy.sync(site, max_products=2)  # interrupted sweep: 2 products synced
    assert first.status == "partial"
    result, fetcher = toy.sync(site, resume_within_hours=48)
    assert result.status == "succeeded"
    fetched = {u for u in fetcher.requests if "/p/" in u}
    assert len(fetched) == 2  # only the two products not synced yet
    assert result.counters["products_seen"] == 4
    with transaction(toy.factory) as s:
        run = s.get(ScrapeRun, result.run_id)
        assert run.stats["products_resumed"] == 2
        assert all(p.last_seen_run_id == run.id for p in s.scalars(select(Product)))


# --------------------------------------------------------------------------- price refresh


def test_price_refresh_prices_only_visits_product_urls_and_keeps_details(toy):
    site = toy_site()
    toy.sync(site)
    _set(site, f"{BASE}/p/A1", title="SHOULD NOT BE STORED", tiers=[{"price": 9.5, "min": 1}])
    result, fetcher = toy.refresh(site, update_details=False)
    assert result.status == "succeeded"
    assert sorted(fetcher.requests) == sorted(f"{BASE}/p/{r}" for r in ["A1", "A2", "A3", "B1"])
    products = toy.products()
    assert products["A1"].title == "Product A1"
    assert [o.price for o in toy.observations("A1")] == [Decimal("10"), Decimal("9.5")]
    with transaction(toy.factory) as s:
        run = s.get(ScrapeRun, result.run_id)
        assert run.operation == "price_refresh" and run.observations_created == 4
        assert {
            o.scrape_run_id
            for o in s.scalars(select(ScrapeObservation).where(ScrapeObservation.price == Decimal("9.5")))
        } == {run.id}


def test_price_refresh_updates_details_by_default(toy):
    site = toy_site()
    toy.sync(site)
    _set(site, f"{BASE}/p/A1", title="New title")
    toy.refresh(site, references=["A1"])
    assert toy.products()["A1"].title == "New title"


def test_retry_failed_products_only(toy):
    site = toy_site()
    toy.sync(site)
    first, _ = toy.refresh(site, failing={f"{BASE}/p/A2"})
    assert first.status == "partial" and first.counters["products_failed"] == 1
    retry, fetcher = toy.refresh(site, retry_failed_run_id=first.run_id)
    assert fetcher.requests == [f"{BASE}/p/A2"]
    assert retry.status == "succeeded"


def test_price_refresh_subset_by_category_and_freshness(toy):
    site = toy_site()
    toy.sync(site)
    _, fetcher = toy.refresh(site, category="CB")
    assert fetcher.requests == [f"{BASE}/p/B1"]
    _, fetcher = toy.refresh(site, skip_fresh_hours=1)
    assert fetcher.requests == []  # everything priced less than an hour ago


def test_identity_mismatch_never_attaches_foreign_prices(toy):
    site = toy_site()
    toy.sync(site)
    _set(site, f"{BASE}/p/A1", ref="ZZZ", tiers=[{"price": 999, "min": 1}])
    result, _ = toy.refresh(site, references=["A1"])
    assert result.status == "partial"
    assert [o.price for o in toy.observations("A1")] == [Decimal("10")]


def test_missing_price_is_distinguished_from_failure_and_keeps_last_known_price(toy):
    site = toy_site()
    toy.sync(site)
    _set(site, f"{BASE}/p/A1", tiers=[])
    result, _ = toy.refresh(site, references=["A1"])
    assert result.status == "succeeded" and result.counters["products_without_price"] == 1
    with transaction(toy.factory) as s:
        item = s.scalar(select(ScrapeRunItem).where(ScrapeRunItem.scrape_run_id == result.run_id))
        assert item.status == "no_price"
        pid = toy.products()["A1"].id
        assert [r["price"] for r in repo.latest_observations(s, pid)] == [Decimal("10")]


def test_latest_and_quantity_queries(toy):
    tiers_v1 = [
        {"price": 3.07, "min": 36, "max": 108},
        {"price": 2.76, "min": 108, "max": 180},
        {"price": 2.29, "min": 180},
    ]
    site = toy_site({"A1": {"category": "A", "tiers": tiers_v1}})
    toy.sync(site)
    _set(site, f"{BASE}/p/A1", tiers=[{"price": 3.10, "min": 36, "max": 108}, {"price": 2.30, "min": 108}])
    toy.refresh(site)
    with transaction(toy.factory) as s:
        pid = toy.products()["A1"].id
        latest = repo.latest_observations(s, pid)
        assert [(r["min_quantity"], r["max_quantity"], r["price"]) for r in latest] == [
            (Decimal(36), Decimal(108), Decimal("3.1")),
            (Decimal(108), None, Decimal("2.3")),
        ]
        assert [r["price"] for r in repo.price_for_quantity(s, pid, Decimal(107))] == [Decimal("3.1")]
        assert [r["price"] for r in repo.price_for_quantity(s, pid, Decimal(108))] == [
            Decimal("2.3")
        ]  # upper bound exclusive
        assert repo.price_for_quantity(s, pid, Decimal(10)) == []  # below the minimum: no fabricated price
        history = repo.price_history(s, pid, Decimal(150))
        assert [o.price for o in history] == [Decimal("2.76"), Decimal("2.3")]
        assert s.scalar(select(func.count(func.distinct(ScrapeObservation.capture_id)))) == 2


# --------------------------------------------------------------------------- Antalis end-to-end


def test_antalis_family_end_to_end_on_fixtures(factory):
    config = load_competitor_config(DEFAULT_COMPETITOR_CONFIG_DIR, "antalis_fr")
    adapter = load_adapter(config)
    family = adapter.parse_listing(
        __import__("tests.conftest", fromlist=["make_page"]).make_page(fixture_text("antalis/family_hq02953.html"))
    )
    pages = {family.category.url: fixture_text("antalis/family_hq02953.html")}
    for link in family.product_links:
        pages[link.url] = fixture_text(f"antalis/sku_{link.reference}.html")
    with transaction(factory) as s:
        competitor_id = repo.upsert_competitor(s, config).id

    result = CatalogSync(adapter, FakeFetcher(pages), factory, competitor_id).run(
        CatalogSyncOptions(root_urls=[family.category.url])
    )
    assert result.status == "succeeded", result.incomplete_reasons
    assert result.counters["products_created"] == 4

    with transaction(factory) as s:
        product = s.scalar(select(Product).where(Product.catalog_reference == "503623"))
        assert product.identity_key == "ref:503623"
        assert product.category.external_id == "HQ02953"
        assert product.category.name == "Caisse Carton à hauteur variable et à fond automatique"
        assert product.normalized_attributes["length_mm"] == 305
        assert product.images[0]["url"].endswith(".jpg")
        obs = repo.latest_observations(s, product.id)
        assert [(o["price"], o["tax_mode"], o["is_promotional"]) for o in obs] == [(Decimal("0.81"), "excl_tax", True)]

    second = CatalogSync(adapter, FakeFetcher(pages), factory, competitor_id).run(
        CatalogSyncOptions(root_urls=[family.category.url])
    )
    assert second.counters["products_created"] == 0 and second.counters["products_updated"] == 4
    refresh = PriceRefresh(adapter, FakeFetcher(pages), factory, competitor_id).run(
        PriceRefreshOptions(references=["503623"])
    )
    assert refresh.status == "succeeded"
    with transaction(factory) as s:
        assert s.scalar(select(func.count()).select_from(Product)) == 4
        pid = s.scalar(select(Product.id).where(Product.catalog_reference == "503623"))
        assert len(repo.price_history(s, pid)) == 3  # 2 catalog runs + 1 price refresh


def test_a_resumed_sync_replays_the_listing_pages_of_the_interrupted_one(toy):
    from competitor_catalog.db.models import ListingPageCache
    from competitor_catalog.services.runs import RunInterrupted

    site = toy_site()

    class StopAfterTwoProducts(RunObserver):
        def progress(self, step, done, goal=None, pending=None):
            if step == "sync_products" and done == 2:
                raise RunInterrupted("redeploy")

    fetcher = FakeFetcher(site)
    with pytest.raises(RunInterrupted):
        CatalogSync(toy.adapter, fetcher, toy.factory, toy.competitor_id, StopAfterTwoProducts()).run(
            CatalogSyncOptions()
        )
    with transaction(toy.factory) as s:
        assert s.scalar(select(func.count()).select_from(ListingPageCache)) == 4
    resumed, fetcher = toy.sync(site, resume_within_hours=48)
    assert resumed.status == "succeeded"
    assert [u for u in fetcher.requests if "/p/" not in u] == []  # no listing page requested again
    assert len(fetcher.requests) == 2  # only the 2 products not synced yet
    assert set(toy.products()) == {"A1", "A2", "A3", "B1"}
    with transaction(toy.factory) as s:
        run = s.get(ScrapeRun, resumed.run_id)
        assert run.full_scope and run.stats["listing_pages_replayed"] == 4 and run.pages_fetched == 2
        assert s.scalar(select(func.count()).select_from(ListingPageCache)) == 0  # completed: purged
        assert s.scalar(select(func.count()).select_from(Product).where(Product.status == "missing")) == 0
