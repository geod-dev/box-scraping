"""Product matching through the HTTP API and the worker (real competitor configs, fake embedder)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from competitor_catalog.api.main import create_app
from competitor_catalog.db.models import Category, Competitor, Product
from competitor_catalog.db.session import transaction
from competitor_catalog.matching.service import MatchingRuntime
from competitor_catalog.services.dispatcher import Dispatcher, DispatcherConfig
from competitor_catalog.settings import DEFAULT_COMPETITOR_CONFIG_DIR, Settings
from tests.conftest import FakeFetcher
from tests.matching_fakes import Runtime, seed_catalogs

pytestmark = pytest.mark.db

API_KEY = "test-key"
SETTINGS = Settings(
    database_url=None,
    scrapedo_api_key=None,
    competitor_config_dir=DEFAULT_COMPETITOR_CONFIG_DIR,
    log_level="INFO",
    html_archive_dir=None,
)


class Env:
    def __init__(self, factory, runtime: MatchingRuntime):
        app = create_app(SETTINGS, factory, api_key=API_KEY, auth_disabled=False, matching=runtime)
        self.client = TestClient(app, headers={"X-API-Key": API_KEY})
        with transaction(factory) as session:
            self.ids = seed_catalogs(session)
        self.dispatcher = Dispatcher(
            factory,
            DEFAULT_COMPETITOR_CONFIG_DIR,
            lambda config: FakeFetcher({}),
            DispatcherConfig(poll_seconds=0),
            write_interval=0,
            matching=runtime,
        )
        self.dispatcher.startup()

    def work(self) -> None:
        self.dispatcher.tick()
        for thread in list(self.dispatcher.threads.values()):
            thread.join(60)
        self.dispatcher.tick()

    def get(self, path, **params):
        response = self.client.get(f"/api/v1{path}", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path, json=None, status=200):
        response = self.client.post(f"/api/v1{path}", json=json)
        assert response.status_code == status, response.text
        return response.json()


@pytest.fixture
def env(factory):
    fake = Runtime()
    env = Env(factory, fake.runtime)
    env.fake = fake
    with env.client:
        yield env


def test_run_review_decide_and_list(env):
    summary = env.get("/matching/summary")
    assert [s["competitor_key"] for s in summary] == ["packdiscount_fr"]  # the reference is not listed
    assert summary[0]["availability"] == {"allowed": True, "reason": None, "message": None}
    assert summary[0]["never_matched"] == 4
    tasks = env.get("/competitors/antalis_fr/worker")["tasks"]
    assert tasks["matching"] == {"allowed": False, "reason": "matching_reference"}

    job = env.post("/matching/competitors/packdiscount_fr/run", {"mode": "incremental"}, status=202)
    assert job["task"] == "matching" and job["estimate"]["products"] == 4
    assert env.post("/matching/competitors/packdiscount_fr/run", status=409)["error"]["code"] == "worker_busy"
    env.work()
    job = env.get(f"/jobs/{job['id']}")
    assert job["status"] == "succeeded", job["error"]
    assert job["progress"]["percent"] == 100 and job["counters"]["processed"] == 4
    assert [s["name"] for s in job["progress"]["steps"]] == ["detect_changes", "embed", "match", "finalize"]

    summary = env.get("/matching/summary", competitor="packdiscount_fr")[0]
    assert summary["counts"]["auto_matched"] == 2 and summary["needs_review"] == 1
    assert summary["counts"]["no_match"] == 1 and summary["last_job"]["id"] == job["id"]
    note = env.get("/notifications", kind="matching_summary")["items"][0]
    assert note["data"]["needs_review"] == 1 and note["job_id"] == job["id"]

    # review queue
    review = env.get("/matching/review")
    assert review["total"] == 1
    item = review["items"][0]
    assert item["product"]["catalog_reference"] == "P2" and item["antalis_product"] is None
    assert item["proposed_antalis_product"]["competitor_key"] == "antalis_fr"
    assert "ambiguous_gap" in {r["code"] for r in item["decision_reasons"]}

    # one product, its candidates and the score breakdown
    detail = env.get(f"/matching/products/{env.ids['P1']}")
    assert detail["status"] == "auto_matched" and detail["antalis_product"]["catalog_reference"] == "A1"
    assert detail["analysis"]["product_type"] == "box" and detail["analysis"]["description_used"] is None
    best = detail["candidates"][0]
    assert best["rank"] == 1 and best["is_associated"] and best["is_proposed"]
    assert best["weights"] == {"semantic": 0.5, "lexical": 0.2, "dimensions": 0.3}
    assert best["dimensions"]["comparable"] and best["dimensions"]["measures"][0]["key"] == "box_mm"
    assert best["semantic"]["cosine"] is not None and best["lexical"]["shared_terms"]
    excluded = [c for c in detail["candidates"] if c["excluded"]]
    assert excluded and excluded[0]["rank"] is None and excluded[0]["exclusions"]
    assert detail["history"][0]["event"] == "computed" and detail["history"][0]["actor"] == "engine"

    # check another product before choosing it, then decide
    compare = env.get(f"/matching/products/{env.ids['P1']}/compare/{env.ids['A2']}")
    assert compare["semantic_available"] and compare["candidate"]["dimension_score"] == 0
    assert compare["candidate"]["differences"][0]["field"] == "box_mm"
    validated = env.post(
        f"/matching/products/{env.ids['P2']}/validate",
        {"antalis_product_id": env.ids["A5"], "decided_by": "alice", "comment": "white"},
    )
    assert validated["status"] == "validated" and validated["antalis_product"]["catalog_reference"] == "A5"
    assert validated["history"][0]["event"] == "validated" and validated["history"][0]["actor"] == "alice"
    rejected = env.post(f"/matching/products/{env.ids['P4']}/reject", {"comment": "not the same film"})
    assert rejected["status"] == "rejected" and rejected["rejected_antalis_products"][0]["catalog_reference"] == "A6"
    bad = env.client.post(
        f"/api/v1/matching/products/{env.ids['P1']}/validate", json={"antalis_product_id": env.ids["P2"]}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_antalis_product"

    # lists and filters
    assert env.get("/matching/review")["total"] == 0
    statuses = {i["product"]["catalog_reference"]: i["status"] for i in env.get("/matching/matches")["items"]}
    assert statuses == {"P1": "auto_matched", "P2": "validated", "P3": "no_match", "P4": "rejected"}
    assert env.get("/matching/matches", decided="true")["total"] == 2
    assert env.get("/matching/matches", manual_choice="true")["total"] in (0, 1)
    assert env.get("/matching/matches", q="caisse")["items"][0]["product"]["catalog_reference"] == "P1"
    assert env.get("/matching/matches", antalis_q="blanc")["items"][0]["product"]["catalog_reference"] == "P2"
    assert env.get("/matching/matches", min_score=90, status="auto_matched")["total"] == 1
    assert env.get("/matching/matches", antalis_product_id=env.ids["A1"])["total"] == 1
    assert env.get("/matching/matches", reason="no_compatible_candidate")["total"] == 1
    page = env.get("/matching/matches", limit=2, offset=2, sort="title", order="asc")
    assert page["total"] == 4 and len(page["items"]) == 2
    assert env.get(f"/products/{env.ids['A1']}")["match_count"] == 1
    assert env.get(f"/products/{env.ids['P1']}")["matching"]["status"] == "auto_matched"
    events = env.get("/matching/history", competitor="packdiscount_fr", event=["validated", "rejected"])
    assert {e["event"] for e in events["items"]} == {"validated", "rejected"}

    # validations survive a full rerun
    env.post("/matching/competitors/packdiscount_fr/run", {"mode": "all"}, status=202)
    env.work()
    assert env.get(f"/matching/products/{env.ids['P2']}")["status"] == "validated"


def test_changed_products_become_deprecated_and_are_rerun(env):
    env.post("/matching/competitors/packdiscount_fr/run", status=202)
    env.work()
    assert env.post("/matching/deprecated/rerun", {}, status=202)["nothing_to_do"] == ["packdiscount_fr"]

    r = env.client.patch(
        f"/api/v1/products/{env.ids['A1']}", json={"title": "Caisse carton simple cannelure 305 x 215 x 105 mm RA4"}
    )
    assert r.status_code == 200
    deprecated = env.get("/matching/deprecated")
    assert deprecated["total"] == 1
    item = deprecated["items"][0]
    assert item["status"] == "deprecated" and item["previous_status"] == "auto_matched"
    assert item["deprecated_reason"] == "antalis_product_changed"
    assert item["antalis_product"]["catalog_reference"] == "A1"  # the former association stays visible
    note = env.get("/notifications", kind="matches_deprecated")["items"][0]
    assert note["level"] == "warning" and note["data"]["matches"][0]["product_id"] == env.ids["P1"]

    result = env.post("/matching/deprecated/rerun", {}, status=202)
    assert [j["options"]["mode"] for j in result["jobs"]] == ["deprecated"] and result["refused"] == []
    env.work()
    job = env.get(f"/jobs/{result['jobs'][0]['id']}")
    assert job["status"] == "succeeded" and job["counters"]["processed"] == 1
    assert env.get("/matching/deprecated")["total"] == 0
    assert env.get(f"/matching/products/{env.ids['P1']}")["status"] == "auto_matched"
    history = env.get("/matching/history", event="deprecated")["items"]
    assert history[0]["data"]["status"] == "auto_matched" and history[0]["product_title"].startswith("Caisse")

    reset = env.post(f"/matching/products/{env.ids['P3']}/reset", {"decided_by": "alice"})
    assert reset["status"] == "deprecated" and reset["deprecated_reason"] == "reset_requested"


def test_refusals_and_config(factory):
    fake = Runtime()
    env = Env(factory, MatchingRuntime(fake.config))  # no VOYAGE_API_KEY
    with env.client:
        config = env.get("/matching/config")
        assert config["configured"] is False and config["reference_competitor"] == "antalis_fr"
        assert config["thresholds"] == {"auto_match": 90.0, "min_gap": 8.0, "review": 75.0}
        r = env.client.post("/api/v1/matching/competitors/packdiscount_fr/run", json={})
        assert r.status_code == 409 and r.json()["error"]["code"] == "matching_not_configured"
        assert env.get("/matching/summary")[0]["availability"]["reason"] == "matching_not_configured"


def test_prerequisites_through_the_api(env):
    r = env.client.post("/api/v1/matching/competitors/antalis_fr/run", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "matching_reference"
    r = env.client.post("/api/v1/matching/competitors/packdiscount_fr/run", json={"mode": "everything"})
    assert r.status_code == 422
    with transaction(env.dispatcher.factory) as session:
        session.execute(update(Competitor).where(Competitor.key == "packdiscount_fr").values(last_full_catalog_at=None))
    r = env.client.post("/api/v1/matching/competitors/packdiscount_fr/run", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "catalog_required"
    worker = env.get("/competitors/packdiscount_fr/worker")
    assert worker["tasks"]["matching"] == {"allowed": False, "reason": "catalog_required"}
    with transaction(env.dispatcher.factory) as session:
        session.execute(
            update(Competitor).where(Competitor.key == "packdiscount_fr").values(last_full_catalog_at=func.now())
        )
    # the generic worker endpoint starts a matching too
    job = env.post("/competitors/packdiscount_fr/worker/start", {"task": "matching"}, status=202)
    assert job["task"] == "matching"
    assert job["options"] == {"mode": "incremental", "reembed": False, "category_id": None}
    estimate = env.post(
        "/competitors/packdiscount_fr/worker/estimate", {"task": "matching", "options": {"mode": "all"}}
    )
    assert estimate["products"] == 4 and estimate["embeddings"] == 11


def test_products_without_dimensions(env):
    page = env.get("/matching/dimensionless", competitor="packdiscount_fr")
    assert [i["catalog_reference"] for i in page["items"]] == ["P3"]  # "Socle banc L. 600 mm": no unit pair
    item = page["items"][0]
    assert item["product_type"] == "furniture" and item["title_has_numbers"] is True
    assert page["by_product_type"] == {"furniture": 1}
    assert env.get("/matching/dimensionless", competitor="packdiscount_fr", product_type="box")["total"] == 0
    assert env.get("/matching/dimensionless", competitor="packdiscount_fr", title_has_numbers="false")["total"] == 0


def _observe(env, product_id: int, price: str, **basis) -> None:
    body = {"product_id": product_id, "price": price, "price_basis": "unit", **basis}
    env.post("/observations", body, status=201)


def test_console_views_of_the_matches(env, factory):
    hub = env.client.app.state.events
    hub.poll()
    env.post("/matching/competitors/packdiscount_fr/run", status=202)
    env.work()
    events = [m.split("\n", 2) for m in hub.poll()]
    updated = [json.loads(data.removeprefix("data: ")) for name, data, _ in events if name == "event: matching.updated"]
    assert [s["competitor_key"] for s in updated] == ["packdiscount_fr"]  # never the reference catalog
    assert updated[0]["counts"]["auto_matched"] == 2 and updated[0]["needs_review"] == 1
    summary = hub.summary["matching"]
    assert summary == {"needs_review": 1, "deprecated": 0}

    # first image in the product briefs
    with transaction(factory) as session:
        session.execute(
            update(Product)
            .where(Product.id == env.ids["A1"])
            .values(images=[{"url": "https://img.test/a1.jpg", "position": 0}, {"url": "https://img.test/2.jpg"}])
        )
    detail = env.get(f"/matching/products/{env.ids['P1']}")
    assert detail["antalis_product"]["image_url"] == "https://img.test/a1.jpg"
    assert detail["product"]["image_url"] is None
    assert detail["candidates"][0]["antalis_product"]["image_url"] == "https://img.test/a1.jpg"

    # price position over the associated pairs (P1 -> A1 is auto_matched)
    position = env.get("/stats/overview")["price_position"]
    assert position == [
        {
            "competitor_key": "packdiscount_fr",
            "pairs": 0,
            "cheaper": 0,
            "same": 0,
            "pricier": 0,
            "median_gap_percent": None,
        }
    ]
    _observe(env, env.ids["A1"], "10", price_unit="unité", price_unit_quantity="1", tax_mode="excl_tax")
    _observe(env, env.ids["P1"], "11", price_unit="UN", tax_mode="unknown")
    position = env.get("/stats/overview")["price_position"][0]
    assert position["pairs"] == 1 and position["pricier"] == 1 and position["median_gap_percent"] == 10.0
    _observe(env, env.ids["P1"], "11", price_unit="UN", price_unit_quantity="1000", tax_mode="unknown")
    assert env.get("/stats/overview")["price_position"][0]["pairs"] == 0  # per 1000: not comparable


def test_list_filtered_by_category_includes_sub_categories(env, factory):
    with transaction(factory) as session:
        competitor_id = session.scalar(select(Competitor.id).where(Competitor.key == "packdiscount_fr"))
        boxes = Category(competitor_id=competitor_id, name="Boxes", url="https://example.test/boxes")
        session.add(boxes)
        session.flush()
        small = Category(
            competitor_id=competitor_id, parent_id=boxes.id, name="Small", url="https://example.test/small"
        )
        films = Category(competitor_id=competitor_id, name="Films", url="https://example.test/films")
        session.add_all([small, films])
        session.flush()
        ids = {"boxes": boxes.id, "small": small.id, "films": films.id}
        session.execute(update(Product).where(Product.id == env.ids["P1"]).values(category_id=small.id))
        session.execute(update(Product).where(Product.id == env.ids["P4"]).values(category_id=films.id))
    env.post("/matching/competitors/packdiscount_fr/run", {"mode": "incremental"}, status=202)
    env.work()

    def refs(**params):
        return {i["product"]["catalog_reference"] for i in env.get("/matching/matches", **params)["items"]}

    assert refs(category_id=ids["boxes"]) == {"P1"}
    assert refs(category_id=ids["small"]) == {"P1"}
    assert refs(category_id=[ids["boxes"], ids["films"]]) == {"P1", "P4"}
    assert len(refs()) == 4
