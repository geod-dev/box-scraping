"""MCP server exposing every API route as a tool: ``POST /api/v1/mcp?api_key=<key>``.

Streamable HTTP transport, stateless, plain JSON responses (no SSE stream). The tools are
generated from the app's OpenAPI schema, and a tool call is replayed in-process against the
app itself (httpx ASGI transport): validation, authentication and logic stay in the routes,
so a new route becomes a tool with no extra code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from competitor_catalog.api.deps import check_api_key

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # newest first
INSTRUCTIONS = """\
Each tool is one endpoint of the competitor catalog REST API (its description starts with
the HTTP method and path). Path and query parameters are top-level arguments, the JSON
request body goes in `body`. The result is the endpoint's JSON response; errors are
`{"error": {"code", "message", "details"}}` with the HTTP status. Lists are paginated
(`limit`/`offset`), decimals are strings. GET tools are read-only."""

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

router = APIRouter()


@dataclass(frozen=True)
class Tool:
    method: str
    path: str
    path_params: tuple[str, ...]
    other_params: dict[str, str]  # name -> "query" | "header"
    has_body: bool
    definition: dict[str, Any]


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- tools from OpenAPI


def build_tools(app: FastAPI) -> dict[str, Tool]:
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})
    tools: dict[str, Tool] = {}
    for path, operations in schema.get("paths", {}).items():
        for method, op in operations.items():
            # tool name = route function name: FastAPI's default operationId is
            # f"{name}{path}_{method}" with non-word characters replaced by "_"
            operation_id = op["operationId"]
            suffix = re.sub(r"\W", "_", path) + "_" + method
            name = operation_id.removesuffix(suffix) or operation_id
            if name in tools:
                name = operation_id
            tools[name] = _tool(name, method.upper(), path, op, components)
    return tools


def _tool(name: str, method: str, path: str, op: dict[str, Any], components: dict[str, Any]) -> Tool:
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    other_params: dict[str, str] = {}
    for param in op.get("parameters", []):
        if param["in"] not in ("path", "query", "header"):
            continue
        prop = dict(param.get("schema", {}))
        if param.get("description"):
            prop["description"] = param["description"]
        properties[param["name"]] = prop
        if param.get("required"):
            required.append(param["name"])
        if param["in"] == "path":
            path_params.append(param["name"])
        else:
            other_params[param["name"]] = param["in"]
    body = op.get("requestBody", {}).get("content", {}).get("application/json")
    if body is not None:
        properties["body"] = {**body["schema"], "description": "JSON request body"}
        if op["requestBody"].get("required"):
            required.append("body")

    input_schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        input_schema["required"] = required
    input_schema = _with_defs(input_schema, components)

    summary = op.get("summary") or name
    description = f"{method} {path}: {summary}"
    if op.get("description"):
        description += "\n\n" + op["description"].strip()
    definition = {
        "name": name,
        "title": summary,
        "description": description,
        "inputSchema": input_schema,
        "annotations": {
            "title": summary,
            "readOnlyHint": method == "GET",
            "destructiveHint": method == "DELETE",
            "idempotentHint": method in ("GET", "PUT", "DELETE"),
            "openWorldHint": False,
        },
    }
    return Tool(method, path, tuple(path_params), other_params, body is not None, definition)


_REF = "#/components/schemas/"


def _with_defs(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    """Self-contained JSON schema: ``#/components/schemas/X`` refs become ``#/$defs/X``."""
    used: dict[str, Any] = {}

    def rewrite(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: rewrite(v) for k, v in node.items()}
            ref = out.get("$ref")
            if isinstance(ref, str) and ref.startswith(_REF):
                key = ref[len(_REF) :]
                out["$ref"] = f"#/$defs/{key}"
                if key not in used:
                    used[key] = None  # placeholder first: recursive models
                    used[key] = rewrite(components[key])
            return out
        if isinstance(node, list):
            return [rewrite(v) for v in node]
        return node

    result = rewrite(schema)
    if used:
        result["$defs"] = used
    return result


def tools_of(app: FastAPI) -> dict[str, Tool]:
    if getattr(app.state, "mcp_tools", None) is None:
        app.state.mcp_tools = build_tools(app)
    return app.state.mcp_tools


# --------------------------------------------------------------------------- tool execution


async def call_tool(app: FastAPI, tool: Tool, arguments: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    known = {*tool.path_params, *tool.other_params, *(("body",) if tool.has_body else ())}
    unknown = sorted(set(arguments) - known)
    if unknown:
        return _text_result(f"unknown argument(s): {', '.join(unknown)}", error=True)
    missing = [p for p in tool.path_params if arguments.get(p) is None]
    if missing:
        return _text_result(f"missing path parameter(s): {', '.join(missing)}", error=True)

    path = tool.path
    for name in tool.path_params:
        path = path.replace(f"{{{name}}}", quote(str(arguments[name]), safe=""))
    params = {k: _query_value(v) for k, v in arguments.items() if tool.other_params.get(k) == "query" and v is not None}
    headers = {k: str(v) for k, v in arguments.items() if tool.other_params.get(k) == "header" and v is not None}
    if api_key:
        headers["X-API-Key"] = api_key
    # raise_app_exceptions=False: an unhandled error is returned as the app's 500 response
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.internal") as client:
        response = await client.request(
            tool.method, path, params=params, headers=headers, json=arguments.get("body") if tool.has_body else None
        )
    text = response.text or f"HTTP {response.status_code} (no content)"
    if response.is_error:
        return _text_result(f"HTTP {response.status_code}: {text}", error=True)
    return _text_result(text)


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [_query_value(v) for v in value]
    return value


def _text_result(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


# --------------------------------------------------------------------------- JSON-RPC


async def _initialize(request: Request, params: dict[str, Any], _api_key: str | None) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    app: FastAPI = request.app
    return {
        "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "competitor-catalog", "title": app.title, "version": app.version},
        "instructions": INSTRUCTIONS,
    }


async def _ping(_request: Request, _params: dict[str, Any], _api_key: str | None) -> dict[str, Any]:
    return {}


async def _list_tools(request: Request, _params: dict[str, Any], _api_key: str | None) -> dict[str, Any]:
    return {"tools": [tool.definition for tool in tools_of(request.app).values()]}


async def _call(request: Request, params: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    name = params.get("name")
    tool = tools_of(request.app).get(name) if isinstance(name, str) else None
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"unknown tool {name!r}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "arguments must be an object")
    return await call_tool(request.app, tool, arguments, api_key)


METHODS = {"initialize": _initialize, "ping": _ping, "tools/list": _list_tools, "tools/call": _call}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


async def _handle(request: Request, message: Any, api_key: str | None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "invalid request")
    method = message.get("method")
    if method is None and ("result" in message or "error" in message):
        return None  # a response from the client: nothing was asked of it
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(message.get("id"), INVALID_REQUEST, "invalid request")
    if "id" not in message:
        return None  # notifications (initialized, cancelled...) need no action from a stateless server
    handler = METHODS.get(method)
    if handler is None:
        return _error(message["id"], METHOD_NOT_FOUND, f"method {method!r} not found")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(message["id"], INVALID_PARAMS, "params must be an object")
    try:
        result = await handler(request, params, api_key)
    except RpcError as exc:
        return _error(message["id"], exc.code, exc.message)
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


@router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(
    request: Request,
    api_key: Annotated[str | None, Query()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Response:
    key = api_key or x_api_key
    check_api_key(request, key)
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return JSONResponse(_error(None, PARSE_ERROR, "parse error"), status_code=400)
    batch = isinstance(payload, list)  # JSON-RPC batches (protocol 2025-03-26)
    replies = [r for m in (payload if batch else [payload]) if (r := await _handle(request, m, key)) is not None]
    if not replies:
        return Response(status_code=202)
    return JSONResponse(replies if batch else replies[0])
