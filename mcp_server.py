"""
DocuMentor MCP Server — v0.4.0
-------------------------------
Exposes SurfSense as MCP tools via the standard MCP protocol (Streamable HTTP).
Compatible with Hermes Agent, Claude Desktop, and any MCP client.

Also serves a JSON-RPC endpoint at /mcp for backward compatibility with the bridge.

Changes from v0.3.0:
  - Rewrote from FastAPI+custom JSON-RPC to FastMCP (real MCP protocol)
  - Streamable HTTP transport (SSE) for Hermes and other MCP clients
  - Backward-compatible /mcp JSON-RPC endpoint for bridge
  - /health endpoint preserved
  - Same 25 tools, same auth logic, same SurfSense client
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SURFSENSE_BASE = os.getenv("SURFSENSE_BASE_URL", "http://backend:8000")
SURFSENSE_EMAIL = os.getenv("SURFSENSE_EMAIL", "admin@documenter.app")
SURFSENSE_PASSWORD = os.getenv("SURFSENSE_PASSWORD", "admin")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
TOKEN_TTL = int(os.getenv("TOKEN_TTL", "3300"))  # 55 min
REQUEST_TIMEOUT = int(os.getenv("MCP_REQUEST_TIMEOUT", "120"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("mcp")

# ---------------------------------------------------------------------------
# FastMCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "DocuMentor",
    host="0.0.0.0",
    port=MCP_PORT,
    stateless_http=True,
)

# ---------------------------------------------------------------------------
# Reusable HTTP client
# ---------------------------------------------------------------------------

_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http


# ---------------------------------------------------------------------------
# Auth — token with TTL + retry on 401
# ---------------------------------------------------------------------------

_token: str | None = None
_token_expires: float = 0


async def authenticate() -> str:
    global _token, _token_expires
    resp = await http().post(
        f"{SURFSENSE_BASE}/auth/jwt/login",
        data={"username": SURFSENSE_EMAIL, "password": SURFSENSE_PASSWORD},
    )
    if resp.status_code != 200:
        _token = None
        raise RuntimeError(f"SurfSense auth failed: {resp.status_code} {resp.text[:200]}")
    _token = resp.json()["access_token"]
    _token_expires = time.time() + TOKEN_TTL
    logger.info("Authenticated with SurfSense (TTL %ds)", TOKEN_TTL)
    return _token


async def get_token() -> str:
    if _token and time.time() < _token_expires:
        return _token
    return await authenticate()


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def authed_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: Any = None,
    data: dict | None = None,
    files: Any = None,
    timeout: float | None = None,
) -> httpx.Response:
    token = await get_token()
    url = f"{SURFSENSE_BASE}{path}"
    kwargs: dict[str, Any] = {"headers": auth_headers(token)}
    if params:
        kwargs["params"] = params
    if json_body is not None:
        kwargs["json"] = json_body
    if data is not None:
        kwargs["data"] = data
    if files is not None:
        kwargs["files"] = files
    if timeout is not None:
        kwargs["timeout"] = timeout

    resp = await getattr(http(), method.lower())(url, **kwargs)

    if resp.status_code == 401:
        logger.warning("Got 401, re-authenticating...")
        token = await authenticate()
        kwargs["headers"] = auth_headers(token)
        resp = await getattr(http(), method.lower())(url, **kwargs)

    resp.raise_for_status()
    return resp


# ===========================================================================
# DOCUMENT TOOLS
# ===========================================================================


@mcp.tool()
async def surfsense_upload(file_path: str, search_space_id: int) -> str:
    """Upload a document (PDF, Excel, Word, CSV, etc.) to SurfSense for indexing. Returns document_ids and queued status."""
    path = Path(file_path)
    if not path.exists():
        raise ValueError(f"File not found: {file_path}")

    mime_type, _ = mimetypes.guess_type(str(path))
    mime_type = mime_type or "application/octet-stream"

    token = await get_token()
    with open(path, "rb") as f:
        files = {"files": (path.name, f, mime_type)}
        form_data = {"search_space_id": str(search_space_id), "should_summarize": "true"}
        resp = await http().post(
            f"{SURFSENSE_BASE}/api/v1/documents/fileupload",
            headers=auth_headers(token),
            files=files,
            data=form_data,
            timeout=120,
        )
    resp.raise_for_status()
    result = resp.json()
    logger.info("Uploaded %s → doc_ids=%s", path.name, result.get("document_ids"))
    return json.dumps({
        "type": "upload_result",
        "file": path.name,
        "document_ids": result.get("document_ids", []),
        "status": "queued",
        "message": result.get("message", "File queued for processing"),
    })


@mcp.tool()
async def surfsense_list_documents(search_space_id: int, page: int = 0, page_size: int = 50) -> str:
    """List all indexed documents in a search space with their status."""
    resp = await authed_request("GET", "/api/v1/documents",
                                params={"search_space_id": search_space_id, "page": page, "page_size": page_size})
    data = resp.json()
    docs = data.get("items", data) if isinstance(data, dict) else data
    return json.dumps({
        "type": "document_list",
        "search_space_id": search_space_id,
        "total": data.get("total", len(docs)) if isinstance(data, dict) else len(docs),
        "documents": [
            {
                "id": d["id"],
                "title": d["title"],
                "type": d.get("document_type", "unknown"),
                "status": d.get("status", {}).get("state", "ready") if isinstance(d.get("status"), dict) else "ready",
                "created_at": d.get("created_at"),
            }
            for d in (docs if isinstance(docs, list) else [])
        ],
    })


@mcp.tool()
async def surfsense_get_document(document_id: int) -> str:
    """Get full detail of a specific document including content and metadata."""
    resp = await authed_request("GET", f"/api/v1/documents/{document_id}")
    doc = resp.json()
    return json.dumps({
        "type": "document_detail",
        "id": doc["id"],
        "title": doc.get("title"),
        "document_type": doc.get("document_type"),
        "content": doc.get("content"),
        "document_metadata": doc.get("document_metadata"),
        "search_space_id": doc.get("search_space_id"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    })


@mcp.tool()
async def surfsense_delete_document(document_id: int) -> str:
    """Permanently delete a document from SurfSense."""
    await authed_request("DELETE", f"/api/v1/documents/{document_id}")
    logger.info("Deleted document %d", document_id)
    return json.dumps({"type": "document_deleted", "id": document_id, "status": "deleted"})


@mcp.tool()
async def surfsense_update_document(document_id: int, title: str | None = None, document_metadata: dict | None = None) -> str:
    """Update a document's title or metadata."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if document_metadata is not None:
        body["document_metadata"] = document_metadata
    if not body:
        raise ValueError("Nothing to update — provide title or document_metadata")
    resp = await authed_request("PUT", f"/api/v1/documents/{document_id}", json_body=body)
    doc = resp.json()
    return json.dumps({"type": "document_updated", "id": doc["id"], "title": doc.get("title"), "updated_at": doc.get("updated_at")})


@mcp.tool()
async def surfsense_document_status(search_space_id: int, document_ids: str) -> str:
    """Batch status check for documents. Pass comma-separated IDs."""
    resp = await authed_request("GET", "/api/v1/documents/status",
                                params={"search_space_id": search_space_id, "document_ids": document_ids})
    data = resp.json()
    items = data.get("items", data) if isinstance(data, dict) else data
    return json.dumps({
        "type": "document_status",
        "items": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "state": item.get("status", {}).get("state", "unknown") if isinstance(item.get("status"), dict) else "unknown",
                "reason": item.get("status", {}).get("reason") if isinstance(item.get("status"), dict) else None,
            }
            for item in (items if isinstance(items, list) else [])
        ],
    })


@mcp.tool()
async def surfsense_search_documents(title: str, search_space_id: int | None = None, page_size: int = 50) -> str:
    """Search documents by title substring."""
    params: dict[str, Any] = {"title": title, "page_size": page_size}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/documents/search", params=params)
    data = resp.json()
    docs = data.get("items", data) if isinstance(data, dict) else data
    return json.dumps({
        "type": "document_search",
        "query": title,
        "total": data.get("total", len(docs)) if isinstance(data, dict) else len(docs),
        "documents": [
            {"id": d["id"], "title": d["title"], "type": d.get("document_type")}
            for d in (docs if isinstance(docs, list) else [])
        ],
    })


@mcp.tool()
async def surfsense_type_counts(search_space_id: int | None = None) -> str:
    """Get document counts grouped by type."""
    params: dict[str, Any] = {}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/documents/type-counts", params=params)
    return json.dumps({"type": "type_counts", "counts": resp.json()})


@mcp.tool()
async def surfsense_extract_tables(doc_id: int, search_space_id: int) -> str:
    """Extract structured data (tables, metrics, statistics) from a document for dashboard rendering."""
    query = (
        "Extract all tables, numeric data, metrics, and statistics from this document. "
        "Return as structured JSON with: summary, tables (array of {title, headers, rows}), "
        "metrics (array of {label, value, unit}), and charts_data (array of {title, type, data})."
    )
    result = await _query_surfsense(query=query, search_space_id=search_space_id)
    result["type"] = "extract_tables_result"
    result["doc_id"] = doc_id
    return json.dumps(result)


# ===========================================================================
# SEARCH SPACE TOOLS
# ===========================================================================


@mcp.tool()
async def surfsense_list_spaces() -> str:
    """List all search spaces (knowledge bases)."""
    resp = await authed_request("GET", "/api/v1/searchspaces")
    spaces = resp.json()
    return json.dumps({
        "type": "search_spaces",
        "spaces": [
            {"id": s["id"], "name": s["name"], "description": s.get("description")}
            for s in (spaces if isinstance(spaces, list) else [])
        ],
    })


@mcp.tool()
async def surfsense_create_space(name: str, description: str = "") -> str:
    """Create a new search space (knowledge base)."""
    resp = await authed_request("POST", "/api/v1/searchspaces", json_body={"name": name, "description": description})
    space = resp.json()
    logger.info("Created space %d: %s", space["id"], space["name"])
    return json.dumps({"type": "search_space_created", "id": space["id"], "name": space["name"]})


@mcp.tool()
async def surfsense_get_space(search_space_id: int) -> str:
    """Get detail of a specific search space."""
    resp = await authed_request("GET", f"/api/v1/searchspaces/{search_space_id}")
    s = resp.json()
    return json.dumps({"type": "search_space_detail", "id": s["id"], "name": s.get("name"),
                        "description": s.get("description"), "created_at": s.get("created_at")})


@mcp.tool()
async def surfsense_update_space(search_space_id: int, name: str | None = None, description: str | None = None) -> str:
    """Update a search space's name or description."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if not body:
        raise ValueError("Nothing to update — provide name or description")
    resp = await authed_request("PUT", f"/api/v1/searchspaces/{search_space_id}", json_body=body)
    s = resp.json()
    return json.dumps({"type": "search_space_updated", "id": s["id"], "name": s.get("name")})


@mcp.tool()
async def surfsense_delete_space(search_space_id: int) -> str:
    """Delete a search space and ALL its documents. Irreversible."""
    await authed_request("DELETE", f"/api/v1/searchspaces/{search_space_id}")
    logger.info("Deleted space %d", search_space_id)
    return json.dumps({"type": "search_space_deleted", "id": search_space_id, "status": "deleted"})


# ===========================================================================
# THREAD / CHAT TOOLS
# ===========================================================================


async def _query_surfsense(query: str, search_space_id: int, thread_id: str | None = None) -> dict:
    """Internal: run a query against SurfSense with streaming."""
    token = await get_token()
    headers = auth_headers(token)
    client = http()

    if not thread_id:
        resp = await client.post(
            f"{SURFSENSE_BASE}/api/v1/threads",
            headers=headers,
            json={"search_space_id": search_space_id, "title": query[:80]},
        )
        resp.raise_for_status()
        thread_id = str(resp.json()["id"])

    full_response = ""
    line_count = 0
    async with client.stream(
        "POST",
        f"{SURFSENSE_BASE}/api/v1/new_chat",
        headers=headers,
        json={
            "chat_id": int(thread_id),
            "user_query": query,
            "search_space_id": search_space_id,
        },
        timeout=120,
    ) as stream:
        async for line in stream.aiter_lines():
            line_count += 1
            # Log first 10 lines and every 50th after that
            if line_count <= 10 or line_count % 50 == 0:
                logger.info("SSE line %d: %r", line_count, line[:200])

            # Handle standard SSE format: "data: ..." lines
            if line.startswith("data:"):
                chunk = line[5:].strip()
            elif line.startswith("event:") or not line.strip():
                # Skip event type lines and empty keepalive lines
                continue
            else:
                # Some endpoints send raw JSON without "data:" prefix
                chunk = line.strip()

            if not chunk or chunk == "[DONE]":
                continue

            try:
                event = json.loads(chunk)
                if isinstance(event, dict):
                    if event.get("type") == "text-delta":
                        full_response += event.get("textDelta", "")
                    elif "content" in event:
                        full_response += str(event["content"])
                    elif "text" in event:
                        full_response += str(event["text"])
                    elif "delta" in event and isinstance(event["delta"], str):
                        full_response += event["delta"]
                    else:
                        # Unknown structure — log and skip
                        logger.info("Unhandled SSE event (line %d): keys=%s first200=%s", line_count, list(event.keys()), str(event)[:200])
                else:
                    # Scalar JSON value (string, number)
                    full_response += str(event)
            except json.JSONDecodeError:
                # Raw text chunk, not JSON
                full_response += chunk

    logger.info("SSE stream finished: %d lines, response_len=%d, first200=%r", line_count, len(full_response), full_response[:200])

    dashboard_data = _parse_dashboard_json(full_response, query)
    logger.info("Query completed (thread=%s, response_len=%d)", thread_id, len(full_response))

    return {
        "type": "query_result",
        "thread_id": thread_id,
        "search_space_id": search_space_id,
        "query": query,
        "dashboard": dashboard_data,
    }


def _parse_dashboard_json(text: str, query: str) -> dict:
    """Try to extract dashboard JSON from response text, fall back to summary."""
    # Greedy match between fences to capture nested {}
    json_match = re.search(r"```(?:json)?\s*(\{.+\})\s*```", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
    # Try as raw JSON
    text_stripped = text.strip()
    if text_stripped.startswith("{") and text_stripped.endswith("}"):
        try:
            parsed = json.loads(text_stripped)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {"type": "summary", "content": text, "query": query}


@mcp.tool()
async def surfsense_query(query: str, search_space_id: int, thread_id: str | None = None) -> str:
    """Query the knowledge base with natural language. Returns structured JSON for dashboards."""
    result = await _query_surfsense(query=query, search_space_id=search_space_id, thread_id=thread_id)
    return json.dumps(result)


@mcp.tool()
async def surfsense_list_threads(search_space_id: int | None = None) -> str:
    """List conversation threads."""
    params: dict[str, Any] = {}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/threads", params=params)
    data = resp.json()
    threads = data if isinstance(data, list) else data.get("items", [])
    return json.dumps({
        "type": "thread_list",
        "threads": [
            {"id": t["id"], "title": t.get("title"), "search_space_id": t.get("search_space_id"),
             "created_at": t.get("created_at")}
            for t in threads
        ],
    })


@mcp.tool()
async def surfsense_get_thread(thread_id: int) -> str:
    """Get detail of a conversation thread."""
    resp = await authed_request("GET", f"/api/v1/threads/{thread_id}")
    t = resp.json()
    return json.dumps({"type": "thread_detail", "id": t["id"], "title": t.get("title"),
                        "search_space_id": t.get("search_space_id"),
                        "created_at": t.get("created_at"), "updated_at": t.get("updated_at")})


@mcp.tool()
async def surfsense_delete_thread(thread_id: int) -> str:
    """Delete a conversation thread."""
    await authed_request("DELETE", f"/api/v1/threads/{thread_id}")
    logger.info("Deleted thread %d", thread_id)
    return json.dumps({"type": "thread_deleted", "id": thread_id, "status": "deleted"})


@mcp.tool()
async def surfsense_thread_history(thread_id: int) -> str:
    """Get all messages in a conversation thread."""
    resp = await authed_request("GET", f"/api/v1/threads/{thread_id}/messages")
    data = resp.json()
    messages = data if isinstance(data, list) else data.get("items", [])
    return json.dumps({
        "type": "thread_history",
        "thread_id": thread_id,
        "messages": [
            {"id": m.get("id"), "role": m.get("role"),
             "content": m.get("content", "")[:500], "created_at": m.get("created_at")}
            for m in messages
        ],
    })


# ===========================================================================
# REPORT TOOLS
# ===========================================================================


@mcp.tool()
async def surfsense_list_reports(search_space_id: int | None = None) -> str:
    """List generated reports."""
    params: dict[str, Any] = {}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/reports", params=params)
    data = resp.json()
    reports = data if isinstance(data, list) else data.get("items", [])
    return json.dumps({
        "type": "report_list",
        "reports": [{"id": r["id"], "title": r.get("title"), "created_at": r.get("created_at")} for r in reports],
    })


@mcp.tool()
async def surfsense_get_report(report_id: int) -> str:
    """Get report content."""
    resp = await authed_request("GET", f"/api/v1/reports/{report_id}/content")
    data = resp.json()
    return json.dumps({"type": "report_content", "id": report_id, "content": data.get("content", data)})


@mcp.tool()
async def surfsense_export_report(report_id: int) -> str:
    """Export a report for download."""
    resp = await authed_request("GET", f"/api/v1/reports/{report_id}/export")
    ct = resp.headers.get("content-type", "")
    if "json" in ct:
        return json.dumps({"type": "report_export", "id": report_id, "data": resp.json()})
    return json.dumps({"type": "report_export", "id": report_id, "content_type": ct,
                        "size_bytes": len(resp.content), "message": "Binary content available via direct download."})


@mcp.tool()
async def surfsense_delete_report(report_id: int) -> str:
    """Delete a report."""
    await authed_request("DELETE", f"/api/v1/reports/{report_id}")
    logger.info("Deleted report %d", report_id)
    return json.dumps({"type": "report_deleted", "id": report_id, "status": "deleted"})


# ===========================================================================
# NOTES TOOL
# ===========================================================================


@mcp.tool()
async def surfsense_create_note(search_space_id: int, content: str) -> str:
    """Create a note in a search space."""
    resp = await authed_request("POST", f"/api/v1/search-spaces/{search_space_id}/notes",
                                json_body={"content": content})
    data = resp.json()
    return json.dumps({"type": "note_created", "id": data.get("id"), "search_space_id": search_space_id})


# ===========================================================================
# LOGS TOOL
# ===========================================================================


@mcp.tool()
async def surfsense_get_logs(search_space_id: int | None = None, limit: int = 50) -> str:
    """Get audit logs."""
    params: dict[str, Any] = {"page_size": limit}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/logs", params=params)
    data = resp.json()
    logs = data if isinstance(data, list) else data.get("items", [])
    return json.dumps({
        "type": "audit_logs",
        "logs": [
            {"id": entry.get("id"), "action": entry.get("action"),
             "details": entry.get("details"), "created_at": entry.get("created_at")}
            for entry in logs[:limit]
        ],
    })


# ===========================================================================
# Legacy JSON-RPC endpoint (backward compat for bridge)
# ===========================================================================

from starlette.requests import Request
from starlette.responses import JSONResponse, Response


# ---------------------------------------------------------------------------
# Custom routes (registered via FastMCP.custom_route)
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health_endpoint(request: Request) -> Response:
    tools = await mcp.list_tools()
    return JSONResponse(content={
        "status": "ok",
        "service": "documenter-mcp",
        "version": "0.4.0",
        "tools": len(tools),
    })


@mcp.custom_route("/jsonrpc", methods=["POST"])
async def legacy_mcp_endpoint(request: Request) -> Response:
    """JSON-RPC endpoint for backward compatibility with the bridge."""
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    req_id = body.get("id")

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "documenter-mcp", "version": "0.4.0"},
            }
        elif method == "tools/list":
            tools = await mcp.list_tools()
            result = {"tools": [
                {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
                for t in tools
            ]}
        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            logger.info("Legacy JSON-RPC call: %s(%s)", tool_name, list(tool_args.keys()))
            t0 = time.time()

            raw_result = await mcp.call_tool(tool_name, tool_args)
            elapsed = time.time() - t0
            logger.info("Tool %s completed in %.1fs", tool_name, elapsed)

            # FastMCP call_tool() returns different things depending on version:
            # - tuple: (list[TextContent], dict_metadata)
            # - list: [TextContent, ...]
            # - str: plain string
            # Extract the actual content blocks first
            if isinstance(raw_result, tuple):
                content_blocks = raw_result[0]  # first element is the list
            else:
                content_blocks = raw_result

            texts = []
            if isinstance(content_blocks, str):
                texts.append(content_blocks)
            elif isinstance(content_blocks, (list, tuple)):
                for c in content_blocks:
                    if hasattr(c, "text"):
                        texts.append(c.text)
                    elif isinstance(c, dict) and "text" in c:
                        texts.append(c["text"])
                    elif isinstance(c, str):
                        texts.append(c)
                    else:
                        texts.append(str(c))
            else:
                # Last resort
                if hasattr(content_blocks, "text"):
                    texts.append(content_blocks.text)
                else:
                    texts.append(str(content_blocks))

            if not texts:
                texts.append("{}")

            result = {"content": [
                {"type": "text", "text": t} for t in texts
            ]}
        elif method == "notifications/initialized":
            return JSONResponse(content=None, status_code=204)
        else:
            return JSONResponse(content={
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            })

        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": result})

    except Exception as e:
        logger.exception("Legacy MCP error")
        return JSONResponse(content={
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32603, "message": str(e)}
        }, status_code=200)


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    logger.info("Starting DocuMentor MCP Server v0.4.0 on port %s", MCP_PORT)
    logger.info("SurfSense backend: %s", SURFSENSE_BASE)
    logger.info("MCP protocol: /mcp (Streamable HTTP)")
    logger.info("Legacy JSON-RPC: /jsonrpc (bridge)")
    logger.info("Health: /health")

    mcp.run(transport="streamable-http")
