"""MCP endpoint: tools generated from the OpenAPI schema, calls replayed against the app."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from competitor_catalog.api.main import create_app
from competitor_catalog.api.mcp import PROTOCOL_VERSIONS, build_tools
from competitor_catalog.settings import load_settings
from tests.test_api import API_KEY, client, config_dir  # noqa: F401 (fixtures)

MCP = f"/api/v1/mcp?api_key={API_KEY}"


def rpc(c: TestClient, method: str, params: dict | None = None, url: str = MCP):
    return c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def call(c: TestClient, name: str, **arguments) -> tuple[bool, str]:
    result = rpc(c, "tools/call", {"name": name, "arguments": arguments}).json()["result"]
    return result["isError"], result["content"][0]["text"]


@pytest.fixture
def offline_app():
    return create_app(load_settings(), sessionmaker(), api_key=API_KEY, sync_competitors=False)


def test_every_route_is_a_tool(offline_app):
    tools = build_tools(offline_app)
    operations = [op for ops in offline_app.openapi()["paths"].values() for op in ops.values()]
    assert len(tools) == len(operations)
    assert all(re.fullmatch(r"[a-z_]{1,64}", name) for name in tools)  # route function names, not operationIds
    assert {"list_products", "start_worker", "health"} <= set(tools)

    for tool in tools.values():
        text = json.dumps(tool.definition["inputSchema"])
        assert "#/components/" not in text  # self-contained: refs point to $defs
        for key in re.findall(r'"#/\$defs/([^"]+)"', text):
            assert key in tool.definition["inputSchema"]["$defs"]

    start = tools["start_worker"].definition
    assert start["inputSchema"]["required"] == ["key", "body"]
    assert start["description"].startswith("POST /api/v1/competitors/{key}/worker/start")
    assert start["annotations"]["readOnlyHint"] is False
    assert tools["list_products"].definition["annotations"]["readOnlyHint"] is True
    assert tools["delete_product"].definition["annotations"]["destructiveHint"] is True


def test_protocol_and_auth(offline_app):
    c = TestClient(offline_app)
    r = rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"
    assert r.json()["result"]["capabilities"] == {"tools": {"listChanged": False}}
    unknown = rpc(c, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert unknown["protocolVersion"] == PROTOCOL_VERSIONS[0]

    assert rpc(c, "ping", url="/api/v1/mcp?api_key=wrong").status_code == 401
    assert rpc(c, "ping", url="/api/v1/mcp").status_code == 401
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    by_header = c.post("/api/v1/mcp", headers={"X-API-Key": API_KEY}, json=ping)
    assert by_header.json()["result"] == {}
    assert c.post(MCP, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert c.get(MCP).status_code == 405
    assert c.post(MCP, content=b"{").json()["error"]["code"] == -32700
    assert rpc(c, "resources/list").json()["error"]["code"] == -32601
    assert rpc(c, "tools/call", {"name": "nope"}).json()["error"]["code"] == -32602

    batch = c.post(MCP, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "x"}])
    assert batch.json() == [{"jsonrpc": "2.0", "id": 1, "result": {}}]

    error, text = call(c, "get_product", product_id="abc")
    assert error and text.startswith("HTTP 422:") and "validation_error" in text
    error, text = call(c, "get_product", product_id=1, extra=2)
    assert error and text == "unknown argument(s): extra"


@pytest.mark.db
def test_tools_call_the_routes(client):  # noqa: F811
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert "list_competitors" in {t["name"] for t in tools}

    error, text = call(client, "list_competitors")
    assert not error and [c["key"] for c in json.loads(text)] == ["toy"]

    error, text = call(client, "list_products", competitor="toy", has_price=False, brand=["a", "b"], limit=5)
    assert not error and json.loads(text)["limit"] == 5

    error, text = call(client, "start_worker", key="toy", body={"task": "price_refresh"})
    assert error and text.startswith("HTTP 409:") and "catalog_required" in text

    error, text = call(client, "get_competitor", key="nope")
    assert error and "not_found" in text
