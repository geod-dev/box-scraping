"""HTTP API against the test database (FastAPI TestClient)."""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import update

from competitor_catalog.api.main import create_app
from competitor_catalog.api.openapi import openapi_schema
from competitor_catalog.core.adapter import load_adapter
from competitor_catalog.core.config import parse_competitor_config
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import ScrapeRun
from competitor_catalog.db.session import transaction
from competitor_catalog.services.catalog_sync import CatalogSync
from competitor_catalog.services.dispatcher import Dispatcher, DispatcherConfig
from competitor_catalog.services.notifications import notify
from competitor_catalog.settings import PROJECT_ROOT, Settings
from tests.conftest import FakeFetcher
from tests.test_jobs_db import TOY_YAML
from tests.toy_adapter import add_extra_categories, toy_site

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
def test_extra_categories_are_flagged_and_show_the_products_they_list(client, factory):
    with transaction(factory) as session:
        competitor_id = repo.get_competitor(session, "toy").id
    site = add_extra_categories(toy_site())
    adapter = load_adapter(parse_competitor_config(TOY_YAML))
    assert CatalogSync(adapter, FakeFetcher(site), factory, competitor_id).run().status == "succeeded"

    (root,) = client.get("/api/v1/categories/tree", params={"competitor": "toy"}).json()
    nodes = {c["name"]: c for c in root["children"]}
    assert root["role"] == "main" and root["subtree_product_count"] == 4 and root["listed_product_count"] == 4
    assert nodes["A"]["role"] == "main" and nodes["A"]["listed_product_count"] == 3
    promo = nodes["Promo"]
    assert (promo["role"], promo["subtree_product_count"], promo["listed_product_count"]) == ("extra", 0, 3)
    pallets = promo["children"][0]
    assert (pallets["name"], pallets["role"], pallets["listed_product_count"]) == ("Pallets", "extra", 2)

    extra = client.get("/api/v1/categories", params={"competitor": "toy", "role": "extra"}).json()
    assert {c["name"] for c in extra["items"]} == {"Promo", "Pallets"}
    detail = client.get(f"/api/v1/categories/{promo['id']}").json()
    assert (detail["role"], detail["subtree_product_count"], detail["listed_product_count"]) == ("extra", 0, 3)

    def refs(**params):
        page = client.get("/api/v1/products", params={"sort": "reference", **params}).json()
        return [p["catalog_reference"] for p in page["items"]]

    assert refs(category_id=promo["id"]) == ["A1", "A2", "B1"]
    assert refs(category_id=promo["id"], include_subcategories=False) == ["A2"]
    assert refs(category_id=pallets["id"]) == ["A1", "B1"]
    facets = client.get("/api/v1/products/facets", params={"category_id": promo["id"]}).json()
    assert facets["total"] == 3

    by_ref = {ref: client.get(f"/api/v1/products/by-reference/toy/{ref}").json() for ref in ("A1", "A2", "A3", "B1")}
    assert [c["name"] for c in by_ref["A1"]["category_path"]] == ["Root", "A"]  # its own category
    assert {ref: [c["name"] for c in p["listed_in"]] for ref, p in by_ref.items()} == {
        "A1": ["Pallets"],
        "A2": ["Promo"],
        "A3": [],
        "B1": ["Pallets"],  # through its category B, listed as a whole
    }


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


def _product_with_prices(client, reference: str, prices: list[tuple[str, str, dict]]) -> dict:
    product = client.post(
        "/api/v1/products",
        json={"competitor": "toy", "url": f"https://shop.toy.test/p/{reference}", "catalog_reference": reference},
    ).json()
    for price, observed_at, extra in prices:
        body = {"product_id": product["id"], "price": price, "observed_at": observed_at, **extra}
        assert client.post("/api/v1/observations", json=body).status_code == 201
    return product


@pytest.mark.db
def test_price_trend_is_the_last_from_price_change(client):
    flat = _product_with_prices(client, "FLAT", [("10", "2026-01-01T00:00:00Z", {})] * 2)
    assert client.get(f"/api/v1/products/{flat['id']}").json()["price_trend"] is None
    product = _product_with_prices(
        client,
        "T",
        [
            ("10", "2026-01-01T00:00:00Z", {}),
            ("12.5", "2026-01-02T00:00:00Z", {}),
            ("12.5", "2026-01-03T00:00:00Z", {}),
            ("99", "2026-01-04T00:00:00Z", {"price_unit_quantity": "1000"}),  # other unit basis: no change
        ],
    )
    trend = client.get(f"/api/v1/products/{product['id']}").json()["price_trend"]
    assert trend["previous_price"] == "10" and trend["change_percent"] == 25.0
    assert datetime.fromisoformat(trend["changed_at"]) == datetime(2026, 1, 2, tzinfo=UTC)
    listed = client.get("/api/v1/products", params={"reference": ["T", "FLAT"], "sort": "reference"}).json()
    assert [p["price_trend"] for p in listed["items"]] == [None, trend]


@pytest.mark.db
def test_stats_overview(client, worker, factory):
    client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync"})
    worker()
    with transaction(factory) as session:  # the fake fetcher costs no Scrape.do credit
        session.execute(update(ScrapeRun).values(credits_used=7))
    stats = client.get("/api/v1/stats/overview", params={"days": 7}).json()
    assert stats["days"] == 7 and len(stats["daily"]) == 7
    dates = [date.fromisoformat(d["date"]) for d in stats["daily"]]
    assert dates == sorted(dates) and dates[-1] - dates[0] == timedelta(days=6)
    today = stats["daily"][-1]
    assert today["observations"] == 4 and today["products_created"] == 4 and today["jobs_succeeded"] == 1
    assert today["credits_used"] == 7 and today["price_changes"] == 0
    assert all(d["observations"] == 0 for d in stats["daily"][:-1])
    totals = stats["totals"]
    assert totals["observations"] == 4 and totals["credits_used"] == 7
    assert totals["jobs"] == {"succeeded": 1, "partial": 0, "failed": 0, "cancelled": 0, "interrupted": 0}
    assert stats["credits"]["monthly_limit"] is None and "SCRAPEDO_API_KEY" in stats["credits"]["error"]
    assert stats["price_position"] == [
        {"competitor_key": "toy", "pairs": 0, "cheaper": 0, "same": 0, "pricier": 0, "median_gap_percent": None}
    ]

    first = client.get("/api/v1/products", params={"competitor": "toy", "sort": "reference"}).json()["items"][0]
    tier = client.get(f"/api/v1/products/{first['id']}/prices").json()[0]  # 10, observed by the sync
    basis = {k: tier[k] for k in ("currency", "price_basis", "price_unit", "price_unit_quantity", "tax_mode")}
    body = {"product_id": first["id"], "price": "8", "observed_at": "2020-01-01T00:00:00Z", **basis}
    client.post("/api/v1/observations", json=body)  # 8 long ago, then 10 today: an increase
    body["observed_at"] = datetime.now(UTC).isoformat()  # then 8 again: a decrease
    client.post("/api/v1/observations", json=body)
    stats = client.get("/api/v1/stats/overview", params={"tz": "Europe/Paris"}).json()
    assert stats["days"] == 30
    assert (stats["totals"]["price_increases"], stats["totals"]["price_decreases"]) == (1, 1)
    assert stats["daily"][-1]["price_changes"] == 2
    r = client.get("/api/v1/stats/overview", params={"tz": "Mars/Olympus"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_time_zone"


def _events(messages: list[str]) -> list[tuple[str, dict]]:
    parsed = []
    for message in messages:
        fields = dict(line.split(": ", 1) for line in message.strip().splitlines())
        parsed.append((fields["event"], json.loads(fields["data"])))
    return parsed


@pytest.mark.db
def test_event_hub_broadcasts_what_changed(client, worker):
    hub = client.app.state.events
    assert hub.poll() == []  # first poll: baseline only
    assert hub.summary == {
        "jobs": [],
        "competitors": [
            {"key": "toy", "name": "Toy competitor", "enabled": True, "worker_state": "idle", "job_id": None}
        ],
        "matching": {"needs_review": 0, "deprecated": 0},
        "notifications": {"unread": 0, "by_level": {}},
    }

    job = client.post("/api/v1/competitors/toy/worker/start", json={"task": "catalog_sync"}).json()
    events = _events(hub.poll())
    assert [name for name, _ in events] == ["job.updated", "competitor.updated", "console.summary"]
    assert events[0][1] == job
    assert events[1][1]["worker"]["state"] == "queued"
    assert [j["id"] for j in events[2][1]["jobs"]] == [job["id"]]
    assert events[2][1]["competitors"][0]["job_id"] == job["id"]
    assert hub.poll() == []  # nothing changed

    worker()
    events = _events(hub.poll())
    assert [name for name, _ in events] == [
        "job.updated",
        "job.finished",
        "notification.created",
        "notification.created",
        "competitor.updated",
        "console.summary",
    ]
    assert events[1][1]["status"] == "succeeded"
    assert [e[1]["kind"] for e in events[2:4]] == ["job_started", "job_succeeded"]
    assert events[4][1]["worker"]["state"] == "idle" and events[4][1]["last_full_catalog_at"]
    assert events[5][1]["jobs"] == [] and events[5][1]["notifications"]["unread"] == 2

    # a job created and finished between two polls is still reported
    job = client.post("/api/v1/competitors/toy/worker/start", json={"task": "price_refresh"}).json()
    client.post(f"/api/v1/jobs/{job['id']}/cancel")
    assert [name for name, _ in _events(hub.poll())][:2] == ["job.updated", "job.finished"]


@pytest.mark.db
def test_event_stream_over_http(factory, config_dir):
    settings = Settings(None, None, config_dir, "INFO", None)
    app = create_app(settings, factory, api_key=API_KEY, auth_disabled=False)
    app.state.events.poll_seconds = 0.1
    app.state.events.heartbeat_seconds = 0.3
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        thread.join(0.05)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", timeout=10) as http:
            assert http.get("/events", headers={"X-API-Key": "wrong"}).status_code == 401
            with http.stream("GET", "/events", headers={"X-API-Key": API_KEY}) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                lines = response.iter_lines()
                assert next(lines) == "retry: 3000"
                assert next(lines) == ""
                assert next(lines) == "event: console.summary"
                assert json.loads(next(lines).removeprefix("data: "))["competitors"][0]["key"] == "toy"
                with transaction(factory) as session:
                    notify(session, level="warning", kind="test", title="Hello")
                seen = set()
                for line in lines:
                    if line in ("event: notification.created", ": ping"):
                        seen.add(line)
                    elif line.startswith("data: ") and '"title":"Hello"' in line:
                        seen.add("data")
                    if len(seen) == 3:
                        break
                assert seen == {"event: notification.created", ": ping", "data"}
        hub = app.state.events
        deadline = time.monotonic() + 5  # the closed stream unsubscribes, then the poller stops
        while (hub._subscribers or not hub._task.done()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not hub._subscribers and hub._task.done()
    finally:
        server.should_exit = True
        thread.join(10)
