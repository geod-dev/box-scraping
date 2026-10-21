"""Banned categories, category-scoped catalog syncs and matchings (PostgreSQL)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from competitor_catalog.db.models import (
    BannedCategory,
    Category,
    Product,
    ProductMatch,
    ScrapeObservation,
    ScrapeRun,
    ScrapeRunItem,
)
from competitor_catalog.db.session import transaction
from competitor_catalog.services import category_bans
from tests.test_api import client, config_dir, worker  # noqa: F401 - fixtures
from tests.test_engine_db import _set, toy  # noqa: F401 - fixture
from tests.test_matching_api import env  # noqa: F401 - fixture
from tests.toy_adapter import BASE, toy_site

pytestmark = pytest.mark.db

CAT_A, CAT_B, ROOT = f"{BASE}/cat-a", f"{BASE}/cat-b", f"{BASE}/root"


def _ban(factory, name: str, **options) -> tuple[int, dict]:
    with transaction(factory) as session:
        category = session.scalar(select(Category).where(Category.name == name))
        ban = category_bans.ban_category(session, category, **options)
        return ban.id, dict(ban.deleted)


def _run_items(factory, run_id: int) -> dict[str, ScrapeRunItem]:
    with transaction(factory) as session:
        items = session.scalars(select(ScrapeRunItem).where(ScrapeRunItem.scrape_run_id == run_id))
        return {item.url: item for item in items}


# --------------------------------------------------------------------------- engine


def test_ban_deletes_the_subtree_and_the_crawls_skip_it(toy):  # noqa: F811
    toy.sync(toy_site())
    ban_id, deleted = _ban(toy.factory, "A", reason="not our market", banned_by="tester")
    assert deleted == {"categories": 1, "products": 3, "matches": 0, "observations": 3, "matches_deprecated": 0}
    assert set(toy.products()) == {"B1"}
    assert toy.count(ScrapeObservation) == 1
    with transaction(toy.factory) as session:
        ban = session.get(BannedCategory, ban_id)
        assert (ban.name, ban.url, ban.external_id, ban.kind, ban.parent_url) == ("A", CAT_A, "CA", "category", ROOT)
        assert (ban.reason, ban.banned_by, ban.depth) == ("not our market", "tester", 1)
        assert {c.name for c in session.scalars(select(Category))} == {"Root", "B"}

    result, fetcher = toy.sync(toy_site())
    assert result.status == "succeeded", result.incomplete_reasons
    assert [u for u in fetcher.requests if u.startswith(CAT_A) or "/p/A" in u] == []  # not even fetched
    assert set(toy.products()) == {"B1"}
    item = _run_items(toy.factory, result.run_id)[CAT_A]
    assert (item.status, item.warnings) == ("skipped", ["in a banned category"])
    with transaction(toy.factory) as session:
        assert session.get(ScrapeRun, result.run_id).stats["banned_listing_pages_skipped"] == 1
        assert {c.name for c in session.scalars(select(Category))} == {"Root", "B"}


def test_a_product_of_a_banned_category_reached_elsewhere_is_not_stored(toy):  # noqa: F811
    site = toy_site()
    toy.sync(site)
    _ban(toy.factory, "B")
    # B1 is now also listed in category A, but its page says it belongs to the banned B
    _set(site, CAT_A, products=[*json.loads(site[CAT_A])["products"], {"url": f"{BASE}/p/B1", "ref": "B1"}])
    result, _ = toy.sync(site)
    assert set(toy.products()) == {"A1", "A2", "A3"}
    assert _run_items(toy.factory, result.run_id)[f"{BASE}/p/B1"].warnings == ["in a banned category"]


def test_unban_recreates_the_category_empty_and_a_category_sync_fills_it(toy):  # noqa: F811
    toy.sync(toy_site())
    ban_id, _ = _ban(toy.factory, "A")
    with transaction(toy.factory) as session:
        category = category_bans.unban_category(session, session.get(BannedCategory, ban_id))
        root = session.scalar(select(Category).where(Category.name == "Root"))
        assert (category.name, category.url, category.external_id, category.parent_id) == ("A", CAT_A, "CA", root.id)
        new_id = category.id
        assert session.get(BannedCategory, ban_id) is None
    assert set(toy.products()) == {"B1"}

    result, fetcher = toy.sync(toy_site(), category_id=new_id)
    assert result.status == "succeeded", result.incomplete_reasons
    assert fetcher.requests[0] == CAT_A
    assert ROOT not in fetcher.requests and CAT_B not in fetcher.requests
    products = toy.products()
    assert {ref: p.category_id for ref, p in products.items() if ref.startswith("A")} == dict.fromkeys(
        ("A1", "A2", "A3"), new_id
    )
    with transaction(toy.factory) as session:
        run = session.get(ScrapeRun, result.run_id)
        assert run.full_scope is False and run.params["category_id"] == new_id and run.products_marked_missing == 0


def test_scoped_listing_pages_are_only_crawled_by_a_partial_crawl(toy):  # noqa: F811
    site = toy_site()
    extra = f"{BASE}/cat-b/products"
    _set(site, CAT_B, scoped=[extra])
    site[extra] = json.dumps(
        {
            "type": "listing",
            "category": {"url": extra, "name": "B products", "ancestors": [ROOT, CAT_B]},
            "products": [{"url": f"{BASE}/p/B1", "ref": "B1"}],
        }
    )
    _, fetcher = toy.sync(site)
    assert extra not in fetcher.requests  # the full crawl reaches the products another way
    with transaction(toy.factory) as session:
        b = session.scalar(select(Category).where(Category.name == "B"))
    _, fetcher = toy.sync(site, category_id=b.id)
    assert extra in fetcher.requests


def test_category_option_cannot_be_combined_with_root_urls(toy):  # noqa: F811
    with pytest.raises(ValueError, match="category_id"):
        toy.sync(toy_site(), category_id=1, root_urls=[ROOT])


# --------------------------------------------------------------------------- API


def _category_id(client, name: str) -> int:  # noqa: F811
    items = client.get("/api/v1/categories", params={"competitor": "toy", "q": name}).json()["items"]
    return next(c["id"] for c in items if c["name"] == name)


def test_ban_unban_and_category_sync_through_the_api(client, worker):  # noqa: F811
    assert client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync"}).status_code == 202
    worker()
    root_id, cat_a = _category_id(client, "Root"), _category_id(client, "A")

    r = client.post(f"/api/v1/categories/{cat_a}/ban", json={"reason": "out of scope"})
    assert r.status_code == 201, r.text
    ban = r.json()
    assert ban["deleted"]["products"] == 3 and ban["category_id"] == cat_a and ban["competitor_key"] == "toy"
    assert ban["parent_category_id"] == root_id and ban["url"] == CAT_A
    assert client.get(f"/api/v1/categories/{cat_a}").status_code == 404
    banned = client.get("/api/v1/categories/banned", params={"competitor": "toy"}).json()
    assert banned["total"] == 1 and banned["items"][0]["id"] == ban["id"]
    assert client.get(f"/api/v1/categories/banned/{ban['id']}").json()["reason"] == "out of scope"
    assert client.get("/api/v1/competitors/toy").json()["stats"]["banned_categories"] == 1
    assert any(n["kind"] == "category_banned" for n in client.get("/api/v1/notifications").json()["items"])

    # a busy worker: no ban, and an unban + scrape is refused as a whole
    busy = client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync"}).json()
    r = client.post(f"/api/v1/categories/{_category_id(client, 'B')}/ban")
    assert r.status_code == 409 and r.json()["error"]["code"] == "worker_busy"
    r = client.post(f"/api/v1/categories/banned/{ban['id']}/unban", json={"scrape": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "worker_busy"
    assert client.get("/api/v1/categories/banned").json()["total"] == 1
    client.post(f"/api/v1/jobs/{busy['id']}/cancel")

    r = client.post(f"/api/v1/categories/banned/{ban['id']}/unban", json={"scrape": True})
    assert r.status_code == 200, r.text
    category, job = r.json()["category"], r.json()["job"]
    assert category["name"] == "A" and category["id"] != cat_a and category["parent_id"] == root_id
    assert job["task"] == "catalog_sync" and job["options"]["category_id"] == category["id"]
    assert client.get("/api/v1/categories/banned").json()["total"] == 0
    worker()
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["status"] == "succeeded"
    products = client.get("/api/v1/products", params={"category_id": category["id"]}).json()
    assert sorted(p["catalog_reference"] for p in products["items"]) == ["A1", "A2", "A3"]

    # a category sync on demand, and its estimate
    r = client.post(f"/api/v1/categories/{category['id']}/scrape", json={"max_products": 1})
    assert r.status_code == 202 and r.json()["options"]["category_id"] == category["id"]
    assert r.json()["estimate"]["products"] == 1
    client.post(f"/api/v1/jobs/{r.json()['id']}/cancel")
    r = client.post(
        "/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync", "options": {"category_id": 999999}}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_options"
    assert client.post("/api/v1/categories/999999/scrape").status_code == 404
    assert client.post("/api/v1/categories/banned/999999/unban").status_code == 404


def test_category_matching_and_banning_a_reference_category(env):  # noqa: F811
    factory = env.dispatcher.factory
    with transaction(factory) as session:
        products = {p.catalog_reference: p for p in session.scalars(select(Product))}
        boxes = Category(competitor_id=products["P1"].competitor_id, name="Boxes", url="https://pd.example/c/boxes")
        reference = Category(competitor_id=products["A1"].competitor_id, name="Cartons", url="https://an.example/c/1")
        session.add_all([boxes, reference])
        session.flush()
        products["P1"].category_id = products["P2"].category_id = boxes.id
        products["A1"].category_id = reference.id
        boxes_id, reference_id = boxes.id, reference.id

    job = env.post(f"/categories/{boxes_id}/match", {"mode": "all"}, status=202)
    assert job["task"] == "matching" and job["options"]["category_id"] == boxes_id
    # while a matching job is active: the reference catalog and the competitor cannot be banned
    r = env.client.post(f"/api/v1/categories/{reference_id}/ban")
    assert r.status_code == 409 and r.json()["error"]["code"] == "matching_running"
    r = env.client.post(f"/api/v1/categories/{boxes_id}/ban")
    assert r.status_code == 409 and r.json()["error"]["code"] == "worker_busy"
    env.work()
    assert env.get(f"/jobs/{job['id']}")["status"] == "succeeded"
    with transaction(factory) as session:
        rows = session.execute(select(Product.catalog_reference, ProductMatch.status).join(ProductMatch.product))
        matched = dict(rows.all())
    assert matched == {"P1": "auto_matched", "P2": "needs_review"}  # P3, P4 are outside the category

    r = env.client.post(f"/api/v1/categories/{reference_id}/match")
    assert r.status_code == 409 and r.json()["error"]["code"] == "matching_reference"

    ban = env.post(f"/categories/{reference_id}/ban", {"banned_by": "tester"}, status=201)
    assert ban["deleted"]["products"] == 1 and ban["deleted"]["matches_deprecated"] == 1
    p1 = env.get(f"/matching/products/{env.ids['P1']}")
    assert p1["status"] == "deprecated" and p1["deprecated_reason"] == "antalis_product_deleted"
