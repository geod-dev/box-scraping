"""HTTP API against the test database (FastAPI TestClient)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from competitor_catalog.api.main import create_app
from competitor_catalog.api.openapi import openapi_schema
from competitor_catalog.services.dispatcher import Dispatcher, DispatcherConfig
from competitor_catalog.settings import PROJECT_ROOT, Settings
from tests.conftest import FakeFetcher
from tests.test_jobs_db import TOY_YAML
from tests.toy_adapter import toy_site

API_KEY = "test-key"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "toy.yaml").write_text(yaml.safe_dump(TOY_YAML), encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(factory, config_dir):
    settings = Settings(
        database_url=None,
        scrapedo_api_key=None,
        competitor_config_dir=config_dir,
        log_level="INFO",
        html_archive_dir=None,
    )
    app = create_app(settings, factory, api_key=API_KEY, auth_disabled=False)
    with TestClient(app, headers={"X-API-Key": API_KEY}) as c:
        yield c


@pytest.fixture
def worker(factory, config_dir):
    dispatcher = Dispatcher(
        factory, config_dir, lambda config: FakeFetcher(toy_site()), DispatcherConfig(poll_seconds=0), write_interval=0
    )
    dispatcher.startup()

    def run():
        dispatcher.tick()
        for thread in list(dispatcher.threads.values()):
            thread.join(30)
        dispatcher.tick()

    return run


@pytest.mark.db
def test_auth_required_except_health(client):
    assert client.get("/api/v1/health").status_code == 200
    r = client.get("/api/v1/competitors", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert client.get("/api/v1/competitors", headers={"Authorization": f"Bearer {API_KEY}"}).status_code == 200


@pytest.mark.db
def test_worker_lifecycle_through_the_api(client, worker):
    competitors = client.get("/api/v1/competitors").json()
    assert [c["key"] for c in competitors] == ["toy"]
    toy = competitors[0]
    assert toy["worker"]["state"] == "idle"
    assert toy["worker"]["tasks"]["catalog_sync"] == {"allowed": True, "reason": None}
    assert toy["worker"]["tasks"]["price_refresh"] == {"allowed": False, "reason": "catalog_required"}

    r = client.post("/api/v1/competitors/toy/worker/start", json={"task": "price_refresh"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "catalog_required"

    detail = client.get("/api/v1/competitors/toy").json()
    assert set(detail["estimates"]) == {"catalog_sync", "price_refresh", "matching"}

    r = client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync"})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["competitor_key"] == "toy"
    assert client.get("/api/v1/competitors/toy/worker").json()["state"] == "queued"

    for task in ("catalog_sync", "price_refresh"):
        r = client.post("/api/v1/competitors/toy/worker/start", json={"task": task})
        assert r.status_code == 409
    assert (
        client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync"}).json()["error"]["code"]
        == "worker_busy"
    )

    worker()
    state = client.get("/api/v1/competitors/toy/worker").json()
    assert state["state"] == "idle" and state["job"] is None
    assert state["last_job"]["status"] == "succeeded"
    assert state["last_job"]["progress"]["percent"] == 100.0
    assert state["tasks"]["price_refresh"]["allowed"] is True
    toy = client.get("/api/v1/competitors/toy").json()
    assert toy["last_full_catalog_at"] is not None
    assert toy["stats"]["products_active"] == 4

    r = client.post("/api/v1/competitors/toy/worker/start", json={"task": "price_refresh", "options": {"limit": 2}})
    assert r.status_code == 202
    stop = client.post("/api/v1/competitors/toy/worker/stop")
    assert stop.status_code == 202 and stop.json()["status"] == "cancelled"
    assert client.post("/api/v1/competitors/toy/worker/stop").json()["error"]["code"] == "worker_idle"

    jobs = client.get("/api/v1/jobs", params={"competitor": "toy"}).json()
    assert jobs["total"] == 2 and [j["status"] for j in jobs["items"]] == ["cancelled", "succeeded"]
    assert client.delete(f"/api/v1/jobs/{jobs['items'][0]['id']}").status_code == 204
    assert client.post(f"/api/v1/jobs/{jobs['items'][1]['id']}/pause").json()["error"]["code"] == "job_not_running"

    assert client.post("/api/v1/competitors/toy/worker/pause").json()["error"]["code"] == "worker_idle"
    client.post("/api/v1/competitors/toy/worker/start", json={"task": "price_refresh"})
    r = client.post("/api/v1/competitors/toy/worker/pause")
    assert r.status_code == 409 and r.json()["error"]["code"] == "job_not_running"  # still queued
    client.post("/api/v1/competitors/toy/worker/stop")

    estimate = client.post("/api/v1/competitors/toy/worker/estimate", json={"task": "price_refresh"}).json()
    assert estimate["products"] == 4 and estimate["requests"] == 4


@pytest.mark.db
def test_invalid_job_options_are_422(client):
    r = client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync", "options": {"nope": 1}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = client.post("/api/v1/competitors/toy/worker/start", json={"task": "dance"})
    assert r.status_code == 422


@pytest.mark.db
def test_catalog_data_read_and_crud(client, worker):
    client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync"})
    worker()

    tree = client.get("/api/v1/categories/tree", params={"competitor": "toy"}).json()
    assert len(tree) == 1 and tree[0]["subtree_product_count"] == 4
    assert {c["name"] for c in tree[0]["children"]} == {"A", "B"}

    page = client.get("/api/v1/products", params={"competitor": "toy", "sort": "reference"}).json()
    assert page["total"] == 4
    first = page["items"][0]
    assert first["catalog_reference"] == "A1"
    assert first["latest_price"]["from_price"] == "10" and first["latest_price"]["displayed"] is True

    found = client.get("/api/v1/products", params={"q": "Product B1"}).json()
    assert [p["catalog_reference"] for p in found["items"]] == ["B1"]
    by_attr = client.get("/api/v1/products", params={"attr": "length_mm:100"}).json()
    assert by_attr["total"] == 4
    assert client.get("/api/v1/products", params={"attr": "length_mm:999"}).json()["total"] == 0
    cat_a = next(c for c in tree[0]["children"] if c["name"] == "A")
    assert client.get("/api/v1/products", params={"category_id": cat_a["id"]}).json()["total"] == 3
    assert client.get("/api/v1/products", params={"category_id": tree[0]["id"]}).json()["total"] == 4

    facets = client.get("/api/v1/products/facets", params={"competitor": "toy"}).json()
    assert facets["total"] == 4 and facets["attributes"]["length_mm"] == [{"value": 100, "count": 4}]

    detail = client.get(f"/api/v1/products/{first['id']}").json()
    assert [c["name"] for c in detail["category_path"]] == ["Root", "A"]
    assert len(detail["latest_prices"]) == 1
    assert client.get("/api/v1/products/by-reference/toy/A1").json()["id"] == first["id"]
    history = client.get(f"/api/v1/products/{first['id']}/prices/history").json()
    assert len(history) == 1
    for_qty = client.get(f"/api/v1/products/{first['id']}/prices/for-quantity", params={"quantity": 5}).json()
    assert for_qty[0]["price"] == "10"

    # product CRUD
    r = client.post(
        "/api/v1/products",
        json={"competitor": "toy", "url": "https://shop.toy.test/p/NEW", "catalog_reference": "NEW", "title": "New"},
    )
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["identity_key"] == "ref:NEW"
    dup = client.post("/api/v1/products", json={"competitor": "toy", "url": "x", "catalog_reference": "NEW"})
    assert dup.status_code == 409
    assert client.patch(f"/api/v1/products/{new['id']}", json={"brand": "Acme"}).json()["brand"] == "Acme"

    # observation CRUD keeps last_price_observed_at consistent
    obs = client.post("/api/v1/observations", json={"product_id": new["id"], "price": "12.345"})
    assert obs.status_code == 201, obs.text
    assert obs.json()["price"] == "12.345" and obs.json()["metadata"]["source"] == "api"
    assert client.get(f"/api/v1/products/{new['id']}").json()["last_price_observed_at"] is not None
    assert client.patch(f"/api/v1/observations/{obs.json()['id']}", json={"price": "11"}).json()["price"] == "11"
    assert client.delete(f"/api/v1/observations/{obs.json()['id']}").status_code == 204
    assert client.get(f"/api/v1/products/{new['id']}").json()["last_price_observed_at"] is None

    # a product never matched has no matching state
    assert client.get(f"/api/v1/products/{new['id']}").json()["matching"] is None
    r = client.get(f"/api/v1/matching/products/{new['id']}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "match_not_found"

    assert client.delete(f"/api/v1/products/{first['id']}").status_code == 204
    assert client.get(f"/api/v1/products/{first['id']}").status_code == 404

    # category CRUD
    r = client.post(
        "/api/v1/categories",
        json={"competitor": "toy", "name": "Manual", "url": "https://shop.toy.test/manual", "parent_id": tree[0]["id"]},
    )
    assert r.status_code == 201, r.text
    manual = r.json()
    assert client.patch(f"/api/v1/categories/{tree[0]['id']}", json={"parent_id": manual["id"]}).status_code == 422
    assert client.patch(f"/api/v1/categories/{manual['id']}", json={"name": "Renamed"}).json()["name"] == "Renamed"
    detail = client.get(f"/api/v1/categories/{manual['id']}").json()
    assert [c["name"] for c in detail["path"]] == ["Root", "Renamed"]
    assert client.delete(f"/api/v1/categories/{manual['id']}").status_code == 204

    # runs
    runs = client.get("/api/v1/runs", params={"competitor": "toy"}).json()
    assert runs["total"] == 1
    run = client.get(f"/api/v1/runs/{runs['items'][0]['id']}").json()
    assert run["item_counts"]["product"]["succeeded"] == 4
    items = client.get(f"/api/v1/runs/{run['id']}/items", params={"item_type": "product"}).json()
    assert items["total"] == 4
    assert client.get("/api/v1/run-items", params={"status": "failed"}).json()["total"] == 0

    stats = client.get("/api/v1/stats").json()
    assert stats["products_active"] == 4  # 3 synced left + 1 manual


@pytest.mark.db
def test_notifications_read_unread(client, worker):
    client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync"})
    worker()
    count = client.get("/api/v1/notifications/unread-count").json()
    assert count == {"unread": 2, "by_level": {"info": 1, "success": 1}}
    items = client.get("/api/v1/notifications").json()["items"]
    assert [n["kind"] for n in items] == ["job_succeeded", "job_started"]
    assert items[0]["read"] is False and items[0]["job_id"] is not None

    read = client.post(f"/api/v1/notifications/{items[0]['id']}/read").json()
    assert read["read"] is True and read["read_at"]
    assert client.get("/api/v1/notifications", params={"unread": True}).json()["total"] == 1
    assert client.post(f"/api/v1/notifications/{items[0]['id']}/unread").json()["read"] is False
    assert client.get("/api/v1/notifications", params={"since_id": items[1]["id"]}).json()["total"] == 1
    assert client.post("/api/v1/notifications/read-all").json() == {"updated": 2}
    assert client.get("/api/v1/notifications/unread-count").json()["unread"] == 0
    assert client.delete(f"/api/v1/notifications/{items[0]['id']}").status_code == 204


@pytest.mark.db
def test_health_reports_the_worker(client, worker):
    worker()
    health = client.get("/api/v1/health").json()
    assert health["database"] is True and health["worker"]["online"] is True and health["status"] == "ok"


@pytest.mark.db
def test_unknown_resources_use_the_error_format(client):
    r = client.get("/api/v1/competitors/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert client.get("/api/v1/nothing-here").json()["error"]["code"] == "not_found"


def test_committed_openapi_schema_is_up_to_date():
    committed = json.loads((PROJECT_ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8"))
    assert committed == openapi_schema(), "run: uv run catalog-scraper export-openapi"


def test_api_refuses_to_start_without_key(config_dir, monkeypatch):
    from competitor_catalog.settings import SettingsError

    monkeypatch.delenv("API_KEY", raising=False)

    settings = Settings(None, None, config_dir, "INFO", None)
    with pytest.raises(SettingsError):
        create_app(settings, object(), api_key=None, auth_disabled=False, sync_competitors=False)


@pytest.mark.db
def test_retry_failed_pages_and_restart_through_the_api(client, worker):
    r = client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync"})
    assert r.status_code == 202
    worker()
    job = client.get(f"/api/v1/jobs/{r.json()['id']}").json()
    assert job["status"] == "succeeded" and job["resumed_from_job_id"] is None
    run_id = job["scrape_run_id"]
    r = client.post(f"/api/v1/runs/{run_id}/retry-failed")
    assert r.status_code == 409 and r.json()["error"]["code"] == "nothing_to_retry"
    r = client.post(f"/api/v1/jobs/{job['id']}/restart")
    assert r.status_code == 409 and r.json()["error"]["code"] == "job_not_restartable"

    r = client.post("/api/v1/competitors/toy/worker/start", json={"task": "price_refresh", "options": {"limit": 1}})
    stopped = client.post(f"/api/v1/jobs/{r.json()['id']}/cancel").json()
    assert stopped["status"] == "cancelled"
    restarted = client.post(f"/api/v1/jobs/{stopped['id']}/restart")
    assert restarted.status_code == 202, restarted.text
    assert restarted.json()["resumed_from_job_id"] == stopped["id"]
    assert restarted.json()["options"]["limit"] == 1 and restarted.json()["options"]["skip_fresh_hours"] > 0

    other = client.post(
        "/api/v1/competitors/toy/worker/start",
        json={"task": "catalog_sync", "options": {"retry_failed_run_id": 999999}},
    )
    assert other.status_code == 422 and other.json()["error"]["code"] == "invalid_options"
