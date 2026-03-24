"""
DocuMentor MCP Wrapper — v0.3.0 (hardened)
-------------------------------------------
Exposes SurfSense as MCP tools via JSON-RPC on localhost:8000/mcp.

Changes from v0.2.0:
  - Token caching with TTL (auto-refresh before expiry).
  - Retry on 401 (re-authenticate once, then fail).
  - Reusable httpx.AsyncClient with connection pooling.
  - Structured logging per operation.
  - Normalized error responses.
  - Type-safe argument handling (no blind **kwargs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SURFSENSE_BASE = os.getenv("SURFSENSE_BASE_URL", "http://localhost:8929")
SURFSENSE_EMAIL = os.getenv("SURFSENSE_EMAIL", "admin@documenter.local")
SURFSENSE_PASSWORD = os.getenv("SURFSENSE_PASSWORD", "admin")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
TOKEN_TTL = int(os.getenv("TOKEN_TTL", "3300"))  # 55 min (JWT usually 1h)
REQUEST_TIMEOUT = int(os.getenv("MCP_REQUEST_TIMEOUT", "120"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("mcp")

app = FastAPI(title="DocuMentor MCP Wrapper", version="0.3.0")

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
    """Get a fresh JWT from SurfSense."""
    global _token, _token_expires
    resp = await http().post(
        f"{SURFSENSE_BASE}/auth/jwt/login",
        data={"username": SURFSENSE_EMAIL, "password": SURFSENSE_PASSWORD},
    )
    if resp.status_code != 200:
        _token = None
        raise HTTPException(status_code=502, detail=f"SurfSense auth failed ({resp.status_code})")
    _token = resp.json()["access_token"]
    _token_expires = time.time() + TOKEN_TTL
    logger.info("Authenticated with SurfSense (TTL %ds)", TOKEN_TTL)
    return _token


async def get_token() -> str:
    """Return cached token or refresh if expired."""
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
    stream: bool = False,
) -> httpx.Response:
    """Make an authenticated request. Retry once on 401."""
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

    if stream:
        # Caller must handle the async context manager
        return http().stream(method, url, **kwargs)

    resp = await getattr(http(), method.lower())(url, **kwargs)

    # Retry on 401
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


async def tool_upload(file_path: str, search_space_id: int) -> dict:
    path = Path(file_path)
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"File not found: {file_path}")

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
    return {
        "type": "upload_result",
        "file": path.name,
        "document_ids": result.get("document_ids", []),
        "status": "queued",
        "message": result.get("message", "File queued for processing"),
    }


async def tool_list_documents(search_space_id: int, page: int = 0, page_size: int = 50) -> dict:
    resp = await authed_request("GET", "/api/v1/documents",
                                params={"search_space_id": search_space_id, "page": page, "page_size": page_size})
    data = resp.json()
    docs = data.get("items", data) if isinstance(data, dict) else data
    return {
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
    }


async def tool_get_document(document_id: int) -> dict:
    resp = await authed_request("GET", f"/api/v1/documents/{document_id}")
    doc = resp.json()
    return {
        "type": "document_detail",
        "id": doc["id"],
        "title": doc.get("title"),
        "document_type": doc.get("document_type"),
        "content": doc.get("content"),
        "document_metadata": doc.get("document_metadata"),
        "search_space_id": doc.get("search_space_id"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


async def tool_delete_document(document_id: int) -> dict:
    await authed_request("DELETE", f"/api/v1/documents/{document_id}")
    logger.info("Deleted document %d", document_id)
    return {"type": "document_deleted", "id": document_id, "status": "deleted"}


async def tool_update_document(document_id: int, title: str | None = None, document_metadata: dict | None = None) -> dict:
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if document_metadata is not None:
        body["document_metadata"] = document_metadata
    if not body:
        raise HTTPException(status_code=400, detail="Nothing to update")
    resp = await authed_request("PUT", f"/api/v1/documents/{document_id}", json_body=body)
    doc = resp.json()
    return {"type": "document_updated", "id": doc["id"], "title": doc.get("title"), "updated_at": doc.get("updated_at")}


async def tool_document_status(search_space_id: int, document_ids: str) -> dict:
    resp = await authed_request("GET", "/api/v1/documents/status",
                                params={"search_space_id": search_space_id, "document_ids": document_ids})
    data = resp.json()
    items = data.get("items", data) if isinstance(data, dict) else data
    return {
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
    }


async def tool_search_documents(title: str, search_space_id: int | None = None, page_size: int = 50) -> dict:
    params: dict[str, Any] = {"title": title, "page_size": page_size}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/documents/search", params=params)
    data = resp.json()
    docs = data.get("items", data) if isinstance(data, dict) else data
    return {
        "type": "document_search",
        "query": title,
        "total": data.get("total", len(docs)) if isinstance(data, dict) else len(docs),
        "documents": [
            {"id": d["id"], "title": d["title"], "type": d.get("document_type")}
            for d in (docs if isinstance(docs, list) else [])
        ],
    }


async def tool_type_counts(search_space_id: int | None = None) -> dict:
    params: dict[str, Any] = {}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/documents/type-counts", params=params)
    return {"type": "type_counts", "counts": resp.json()}


async def tool_extract_tables(doc_id: int, search_space_id: int) -> dict:
    query = (
        "Extract all tables, numeric data, metrics, and statistics from this document. "
        "Return as structured JSON with: summary, tables (array of {title, headers, rows}), "
        "metrics (array of {label, value, unit}), and charts_data (array of {title, type, data})."
    )
    result = await tool_query(query=query, search_space_id=search_space_id)
    result["type"] = "extract_tables_result"
    result["doc_id"] = doc_id
    return result


# ===========================================================================
# SEARCH SPACE TOOLS
# ===========================================================================


async def tool_list_spaces() -> dict:
    resp = await authed_request("GET", "/api/v1/searchspaces")
    spaces = resp.json()
    return {
        "type": "search_spaces",
        "spaces": [
            {"id": s["id"], "name": s["name"], "description": s.get("description")}
            for s in (spaces if isinstance(spaces, list) else [])
        ],
    }


async def tool_create_space(name: str, description: str = "") -> dict:
    resp = await authed_request("POST", "/api/v1/searchspaces", json_body={"name": name, "description": description})
    space = resp.json()
    logger.info("Created space %d: %s", space["id"], space["name"])
    return {"type": "search_space_created", "id": space["id"], "name": space["name"]}


async def tool_get_space(search_space_id: int) -> dict:
    resp = await authed_request("GET", f"/api/v1/searchspaces/{search_space_id}")
    s = resp.json()
    return {"type": "search_space_detail", "id": s["id"], "name": s.get("name"),
            "description": s.get("description"), "created_at": s.get("created_at")}


async def tool_update_space(search_space_id: int, name: str | None = None, description: str | None = None) -> dict:
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if not body:
        raise HTTPException(status_code=400, detail="Nothing to update")
    resp = await authed_request("PUT", f"/api/v1/searchspaces/{search_space_id}", json_body=body)
    s = resp.json()
    return {"type": "search_space_updated", "id": s["id"], "name": s.get("name")}


async def tool_delete_space(search_space_id: int) -> dict:
    await authed_request("DELETE", f"/api/v1/searchspaces/{search_space_id}")
    logger.info("Deleted space %d", search_space_id)
    return {"type": "search_space_deleted", "id": search_space_id, "status": "deleted"}


# ===========================================================================
# THREAD / CHAT TOOLS
# ===========================================================================


async def tool_query(query: str, search_space_id: int, thread_id: str | None = None) -> dict:
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
    async with client.stream(
        "POST",
        f"{SURFSENSE_BASE}/api/v1/threads/{thread_id}/messages",
        headers=headers,
        json={"search_space_id": search_space_id, "message": query, "stream": True},
        timeout=120,
    ) as stream:
        async for line in stream.aiter_lines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    try:
                        event = json.loads(chunk)
                        if event.get("type") == "text-delta":
                            full_response += event.get("textDelta", "")
                        elif isinstance(event, dict) and "content" in event:
                            full_response += str(event["content"])
                    except json.JSONDecodeError:
                        full_response += chunk

    # Parse dashboard JSON from response
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
    """Attempt to extract structured dashboard JSON from model response.
    Falls back to a summary object if parsing fails."""
    # Try fenced JSON block first
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try raw JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: wrap as summary
    return {"type": "summary", "content": text, "query": query}


async def tool_list_threads(search_space_id: int | None = None) -> dict:
    params: dict[str, Any] = {}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/threads", params=params)
    data = resp.json()
    threads = data if isinstance(data, list) else data.get("items", [])
    return {
        "type": "thread_list",
        "threads": [
            {"id": t["id"], "title": t.get("title"), "search_space_id": t.get("search_space_id"),
             "created_at": t.get("created_at")}
            for t in threads
        ],
    }


async def tool_get_thread(thread_id: int) -> dict:
    resp = await authed_request("GET", f"/api/v1/threads/{thread_id}")
    t = resp.json()
    return {"type": "thread_detail", "id": t["id"], "title": t.get("title"),
            "search_space_id": t.get("search_space_id"),
            "created_at": t.get("created_at"), "updated_at": t.get("updated_at")}


async def tool_delete_thread(thread_id: int) -> dict:
    await authed_request("DELETE", f"/api/v1/threads/{thread_id}")
    logger.info("Deleted thread %d", thread_id)
    return {"type": "thread_deleted", "id": thread_id, "status": "deleted"}


async def tool_thread_history(thread_id: int) -> dict:
    resp = await authed_request("GET", f"/api/v1/threads/{thread_id}/messages")
    data = resp.json()
    messages = data if isinstance(data, list) else data.get("items", [])
    return {
        "type": "thread_history",
        "thread_id": thread_id,
        "messages": [
            {"id": m.get("id"), "role": m.get("role"),
             "content": m.get("content", "")[:500], "created_at": m.get("created_at")}
            for m in messages
        ],
    }


# ===========================================================================
# REPORT TOOLS
# ===========================================================================


async def tool_list_reports(search_space_id: int | None = None) -> dict:
    params: dict[str, Any] = {}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/reports", params=params)
    data = resp.json()
    reports = data if isinstance(data, list) else data.get("items", [])
    return {
        "type": "report_list",
        "reports": [{"id": r["id"], "title": r.get("title"), "created_at": r.get("created_at")} for r in reports],
    }


async def tool_get_report(report_id: int) -> dict:
    resp = await authed_request("GET", f"/api/v1/reports/{report_id}/content")
    data = resp.json()
    return {"type": "report_content", "id": report_id, "content": data.get("content", data)}


async def tool_export_report(report_id: int) -> dict:
    resp = await authed_request("GET", f"/api/v1/reports/{report_id}/export")
    ct = resp.headers.get("content-type", "")
    if "json" in ct:
        return {"type": "report_export", "id": report_id, "data": resp.json()}
    return {"type": "report_export", "id": report_id, "content_type": ct,
            "size_bytes": len(resp.content), "message": "Binary content available via direct download."}


async def tool_delete_report(report_id: int) -> dict:
    await authed_request("DELETE", f"/api/v1/reports/{report_id}")
    logger.info("Deleted report %d", report_id)
    return {"type": "report_deleted", "id": report_id, "status": "deleted"}


# ===========================================================================
# NOTES TOOL
# ===========================================================================


async def tool_create_note(search_space_id: int, content: str) -> dict:
    resp = await authed_request("POST", f"/api/v1/search-spaces/{search_space_id}/notes",
                                json_body={"content": content})
    data = resp.json()
    return {"type": "note_created", "id": data.get("id"), "search_space_id": search_space_id}


# ===========================================================================
# LOGS TOOL
# ===========================================================================


async def tool_get_logs(search_space_id: int | None = None, limit: int = 50) -> dict:
    params: dict[str, Any] = {"page_size": limit}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id
    resp = await authed_request("GET", "/api/v1/logs", params=params)
    data = resp.json()
    logs = data if isinstance(data, list) else data.get("items", [])
    return {
        "type": "audit_logs",
        "logs": [
            {"id": entry.get("id"), "action": entry.get("action"),
             "details": entry.get("details"), "created_at": entry.get("created_at")}
            for entry in logs[:limit]
        ],
    }


# ===========================================================================
# MCP TOOL DEFINITIONS (unchanged schema, just consolidated)
# ===========================================================================

TOOL_DEFINITIONS = [
    {"name": "surfsense_upload", "description": "Upload a document (PDF, Excel, Word, CSV, etc.) to SurfSense for indexing.",
     "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to the file"}, "search_space_id": {"type": "integer", "description": "Target search space ID"}}, "required": ["file_path", "search_space_id"]}},
    {"name": "surfsense_list_documents", "description": "List all indexed documents in a search space.",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}, "page": {"type": "integer", "default": 0}, "page_size": {"type": "integer", "default": 50}}, "required": ["search_space_id"]}},
    {"name": "surfsense_get_document", "description": "Get full detail of a specific document.",
     "inputSchema": {"type": "object", "properties": {"document_id": {"type": "integer"}}, "required": ["document_id"]}},
    {"name": "surfsense_delete_document", "description": "Permanently delete a document.",
     "inputSchema": {"type": "object", "properties": {"document_id": {"type": "integer"}}, "required": ["document_id"]}},
    {"name": "surfsense_update_document", "description": "Update a document's title or metadata.",
     "inputSchema": {"type": "object", "properties": {"document_id": {"type": "integer"}, "title": {"type": "string"}, "document_metadata": {"type": "object"}}, "required": ["document_id"]}},
    {"name": "surfsense_document_status", "description": "Batch status check for documents (comma-separated IDs).",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}, "document_ids": {"type": "string"}}, "required": ["search_space_id", "document_ids"]}},
    {"name": "surfsense_search_documents", "description": "Search documents by title substring.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "search_space_id": {"type": "integer"}, "page_size": {"type": "integer", "default": 50}}, "required": ["title"]}},
    {"name": "surfsense_type_counts", "description": "Get document counts grouped by type.",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}}, "required": []}},
    {"name": "surfsense_extract_tables", "description": "Extract structured data from a document for dashboard rendering.",
     "inputSchema": {"type": "object", "properties": {"doc_id": {"type": "integer"}, "search_space_id": {"type": "integer"}}, "required": ["doc_id", "search_space_id"]}},
    {"name": "surfsense_list_spaces", "description": "List all search spaces (knowledge bases).",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "surfsense_create_space", "description": "Create a new search space.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}}, "required": ["name"]}},
    {"name": "surfsense_get_space", "description": "Get detail of a specific search space.",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}}, "required": ["search_space_id"]}},
    {"name": "surfsense_update_space", "description": "Update a search space's name or description.",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"}}, "required": ["search_space_id"]}},
    {"name": "surfsense_delete_space", "description": "Delete a search space and ALL its documents. Irreversible.",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}}, "required": ["search_space_id"]}},
    {"name": "surfsense_query", "description": "Query the knowledge base with natural language. Returns structured JSON for dashboards.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "search_space_id": {"type": "integer"}, "thread_id": {"type": "string"}}, "required": ["query", "search_space_id"]}},
    {"name": "surfsense_list_threads", "description": "List conversation threads.",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}}, "required": []}},
    {"name": "surfsense_get_thread", "description": "Get detail of a conversation thread.",
     "inputSchema": {"type": "object", "properties": {"thread_id": {"type": "integer"}}, "required": ["thread_id"]}},
    {"name": "surfsense_delete_thread", "description": "Delete a conversation thread.",
     "inputSchema": {"type": "object", "properties": {"thread_id": {"type": "integer"}}, "required": ["thread_id"]}},
    {"name": "surfsense_thread_history", "description": "Get all messages in a conversation thread.",
     "inputSchema": {"type": "object", "properties": {"thread_id": {"type": "integer"}}, "required": ["thread_id"]}},
    {"name": "surfsense_list_reports", "description": "List generated reports.",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}}, "required": []}},
    {"name": "surfsense_get_report", "description": "Get report content.",
     "inputSchema": {"type": "object", "properties": {"report_id": {"type": "integer"}}, "required": ["report_id"]}},
    {"name": "surfsense_export_report", "description": "Export a report for download.",
     "inputSchema": {"type": "object", "properties": {"report_id": {"type": "integer"}}, "required": ["report_id"]}},
    {"name": "surfsense_delete_report", "description": "Delete a report.",
     "inputSchema": {"type": "object", "properties": {"report_id": {"type": "integer"}}, "required": ["report_id"]}},
    {"name": "surfsense_create_note", "description": "Create a note in a search space.",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}, "content": {"type": "string"}}, "required": ["search_space_id", "content"]}},
    {"name": "surfsense_get_logs", "description": "Get audit logs.",
     "inputSchema": {"type": "object", "properties": {"search_space_id": {"type": "integer"}, "limit": {"type": "integer", "default": 50}}, "required": []}},
]


# ===========================================================================
# TOOL DISPATCHER — explicit routing, no **kwargs
# ===========================================================================

_TOOL_MAP = {
    "surfsense_upload": lambda a: tool_upload(file_path=a["file_path"], search_space_id=a["search_space_id"]),
    "surfsense_list_documents": lambda a: tool_list_documents(search_space_id=a["search_space_id"], page=a.get("page", 0), page_size=a.get("page_size", 50)),
    "surfsense_get_document": lambda a: tool_get_document(document_id=a["document_id"]),
    "surfsense_delete_document": lambda a: tool_delete_document(document_id=a["document_id"]),
    "surfsense_update_document": lambda a: tool_update_document(document_id=a["document_id"], title=a.get("title"), document_metadata=a.get("document_metadata")),
    "surfsense_document_status": lambda a: tool_document_status(search_space_id=a["search_space_id"], document_ids=a["document_ids"]),
    "surfsense_search_documents": lambda a: tool_search_documents(title=a["title"], search_space_id=a.get("search_space_id"), page_size=a.get("page_size", 50)),
    "surfsense_type_counts": lambda a: tool_type_counts(search_space_id=a.get("search_space_id")),
    "surfsense_extract_tables": lambda a: tool_extract_tables(doc_id=a["doc_id"], search_space_id=a["search_space_id"]),
    "surfsense_list_spaces": lambda a: tool_list_spaces(),
    "surfsense_create_space": lambda a: tool_create_space(name=a["name"], description=a.get("description", "")),
    "surfsense_get_space": lambda a: tool_get_space(search_space_id=a["search_space_id"]),
    "surfsense_update_space": lambda a: tool_update_space(search_space_id=a["search_space_id"], name=a.get("name"), description=a.get("description")),
    "surfsense_delete_space": lambda a: tool_delete_space(search_space_id=a["search_space_id"]),
    "surfsense_query": lambda a: tool_query(query=a["query"], search_space_id=a["search_space_id"], thread_id=a.get("thread_id")),
    "surfsense_list_threads": lambda a: tool_list_threads(search_space_id=a.get("search_space_id")),
    "surfsense_get_thread": lambda a: tool_get_thread(thread_id=a["thread_id"]),
    "surfsense_delete_thread": lambda a: tool_delete_thread(thread_id=a["thread_id"]),
    "surfsense_thread_history": lambda a: tool_thread_history(thread_id=a["thread_id"]),
    "surfsense_list_reports": lambda a: tool_list_reports(search_space_id=a.get("search_space_id")),
    "surfsense_get_report": lambda a: tool_get_report(report_id=a["report_id"]),
    "surfsense_export_report": lambda a: tool_export_report(report_id=a["report_id"]),
    "surfsense_delete_report": lambda a: tool_delete_report(report_id=a["report_id"]),
    "surfsense_create_note": lambda a: tool_create_note(search_space_id=a["search_space_id"], content=a["content"]),
    "surfsense_get_logs": lambda a: tool_get_logs(search_space_id=a.get("search_space_id"), limit=a.get("limit", 50)),
}


async def dispatch_tool(name: str, args: dict) -> Any:
    handler = _TOOL_MAP.get(name)
    if not handler:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")
    return await handler(args)


# ===========================================================================
# MCP JSON-RPC ENDPOINT
# ===========================================================================


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    req_id = body.get("id")

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "documenter-mcp", "version": "0.3.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOL_DEFINITIONS}
        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            logger.info("Tool call: %s(%s)", tool_name, list(tool_args.keys()))
            t0 = time.time()
            data = await dispatch_tool(tool_name, tool_args)
            elapsed = time.time() - t0
            logger.info("Tool %s completed in %.1fs", tool_name, elapsed)
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}
        elif method == "notifications/initialized":
            return JSONResponse(content=None, status_code=204)
        else:
            return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})

        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": result})

    except HTTPException as e:
        logger.error("Tool error: %s", e.detail)
        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": e.detail}}, status_code=200)
    except KeyError as e:
        logger.error("Missing required argument: %s", e)
        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Missing required argument: {e}"}}, status_code=200)
    except Exception as e:
        logger.exception("Unexpected error in MCP handler")
        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}, status_code=200)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "documenter-mcp", "version": "0.3.0", "tools": len(TOOL_DEFINITIONS)}


@app.get("/tools")
async def list_tools() -> dict:
    return {"total": len(TOOL_DEFINITIONS), "tools": [{"name": t["name"], "description": t["description"]} for t in TOOL_DEFINITIONS]}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("shutdown")
async def shutdown():
    global _http
    if _http and not _http.is_closed:
        await _http.aclose()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting DocuMentor MCP Wrapper v0.3.0 on port %s", MCP_PORT)
    logger.info("SurfSense backend: %s", SURFSENSE_BASE)
    logger.info("Registered %d tools", len(TOOL_DEFINITIONS))
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="info")
