"""
DocuMentor MCP Wrapper
----------------------
Exposes SurfSense as MCP tools for Hermes Agent.
Runs as an HTTP server on localhost:8000/mcp

25 Tools organized by category:

DOCUMENTS (7):
  - surfsense_upload          — Upload files for indexing
  - surfsense_list_documents  — List docs in a search space
  - surfsense_get_document    — Get full document detail
  - surfsense_delete_document — Delete a document
  - surfsense_update_document — Update title/metadata
  - surfsense_document_status — Batch status check (processing/ready)
  - surfsense_search_documents — Search by title
  - surfsense_type_counts     — Count docs by type
  - surfsense_extract_tables  — Extract structured data

SEARCH SPACES (5):
  - surfsense_list_spaces     — List all knowledge bases
  - surfsense_create_space    — Create new knowledge base
  - surfsense_get_space       — Get space detail
  - surfsense_update_space    — Update name/description
  - surfsense_delete_space    — Delete a space

THREADS/CHAT (5):
  - surfsense_query           — Query knowledge base (creates thread + streams)
  - surfsense_list_threads    — List conversation threads
  - surfsense_get_thread      — Get thread detail
  - surfsense_delete_thread   — Delete a thread
  - surfsense_thread_history  — Get messages in a thread

REPORTS (4):
  - surfsense_list_reports    — List generated reports
  - surfsense_get_report      — Get report content
  - surfsense_export_report   — Export report (PDF/etc)
  - surfsense_delete_report   — Delete a report

NOTES (1):
  - surfsense_create_note     — Create a note in a search space

LOGS (1):
  - surfsense_get_logs        — Get audit logs
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("documenter-mcp")

app = FastAPI(title="DocuMentor MCP Wrapper", version="0.2.0")

# ---------------------------------------------------------------------------
# SurfSense auth (JWT token cache)
# ---------------------------------------------------------------------------

_token_cache: dict[str, str] = {}


async def get_token(client: httpx.AsyncClient) -> str:
    """Authenticate with SurfSense and cache the JWT token."""
    if "token" in _token_cache:
        return _token_cache["token"]

    resp = await client.post(
        f"{SURFSENSE_BASE}/auth/jwt/login",
        data={"username": SURFSENSE_EMAIL, "password": SURFSENSE_PASSWORD},
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"SurfSense auth failed: {resp.status_code} {resp.text}",
        )
    token = resp.json()["access_token"]
    _token_cache["token"] = token
    return token


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# DOCUMENT TOOLS
# ===========================================================================


async def tool_upload(file_path: str, search_space_id: int) -> dict:
    """Upload a document to SurfSense for indexing."""
    path = Path(file_path)
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"File not found: {file_path}")

    mime_type, _ = mimetypes.guess_type(str(path))
    mime_type = mime_type or "application/octet-stream"

    async with httpx.AsyncClient(timeout=120) as client:
        token = await get_token(client)
        with open(path, "rb") as f:
            files = {"files": (path.name, f, mime_type)}
            data = {"search_space_id": str(search_space_id), "should_summarize": "true"}
            resp = await client.post(
                f"{SURFSENSE_BASE}/api/v1/documents/fileupload",
                headers=auth_headers(token),
                files=files,
                data=data,
            )
        resp.raise_for_status()
        result = resp.json()
        return {
            "type": "upload_result",
            "file": path.name,
            "document_ids": result.get("document_ids", []),
            "status": "queued",
            "message": result.get("message", "File queued for processing"),
        }


async def tool_list_documents(search_space_id: int, page: int = 0, page_size: int = 50) -> dict:
    """List documents in a search space."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/documents",
            headers=auth_headers(token),
            params={"search_space_id": search_space_id, "page": page, "page_size": page_size},
        )
        resp.raise_for_status()
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
                for d in docs
            ],
        }


async def tool_get_document(document_id: int) -> dict:
    """Get full detail of a specific document."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/documents/{document_id}",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
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
    """Delete a document by ID."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.delete(
            f"{SURFSENSE_BASE}/api/v1/documents/{document_id}",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        return {"type": "document_deleted", "id": document_id, "status": "deleted"}


async def tool_update_document(document_id: int, title: str | None = None, document_metadata: dict | None = None) -> dict:
    """Update a document's title or metadata."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if document_metadata is not None:
        body["document_metadata"] = document_metadata
    if not body:
        raise HTTPException(status_code=400, detail="Nothing to update — provide title or document_metadata")

    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.put(
            f"{SURFSENSE_BASE}/api/v1/documents/{document_id}",
            headers=auth_headers(token),
            json=body,
        )
        resp.raise_for_status()
        doc = resp.json()
        return {
            "type": "document_updated",
            "id": doc["id"],
            "title": doc.get("title"),
            "updated_at": doc.get("updated_at"),
        }


async def tool_document_status(search_space_id: int, document_ids: str) -> dict:
    """Batch status check for documents (comma-separated IDs)."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/documents/status",
            headers=auth_headers(token),
            params={"search_space_id": search_space_id, "document_ids": document_ids},
        )
        resp.raise_for_status()
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
    """Search documents by title substring."""
    params: dict[str, Any] = {"title": title, "page_size": page_size}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id

    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/documents/search",
            headers=auth_headers(token),
            params=params,
        )
        resp.raise_for_status()
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
    """Get document counts by type."""
    params: dict[str, Any] = {}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id

    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/documents/type-counts",
            headers=auth_headers(token),
            params=params,
        )
        resp.raise_for_status()
        return {"type": "type_counts", "counts": resp.json()}


async def tool_extract_tables(doc_id: int, search_space_id: int) -> dict:
    """Extract structured tables and metrics from a document via targeted query."""
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
    """List all available search spaces."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/searchspaces",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        spaces = resp.json()
        return {
            "type": "search_spaces",
            "spaces": [
                {"id": s["id"], "name": s["name"], "description": s.get("description")}
                for s in (spaces if isinstance(spaces, list) else [])
            ],
        }


async def tool_create_space(name: str, description: str = "") -> dict:
    """Create a new search space."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.post(
            f"{SURFSENSE_BASE}/api/v1/searchspaces",
            headers=auth_headers(token),
            json={"name": name, "description": description},
        )
        resp.raise_for_status()
        space = resp.json()
        return {"type": "search_space_created", "id": space["id"], "name": space["name"]}


async def tool_get_space(search_space_id: int) -> dict:
    """Get detail of a specific search space."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/searchspaces/{search_space_id}",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        s = resp.json()
        return {
            "type": "search_space_detail",
            "id": s["id"],
            "name": s.get("name"),
            "description": s.get("description"),
            "created_at": s.get("created_at"),
        }


async def tool_update_space(search_space_id: int, name: str | None = None, description: str | None = None) -> dict:
    """Update a search space's name or description."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if not body:
        raise HTTPException(status_code=400, detail="Nothing to update — provide name or description")

    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.put(
            f"{SURFSENSE_BASE}/api/v1/searchspaces/{search_space_id}",
            headers=auth_headers(token),
            json=body,
        )
        resp.raise_for_status()
        s = resp.json()
        return {"type": "search_space_updated", "id": s["id"], "name": s.get("name")}


async def tool_delete_space(search_space_id: int) -> dict:
    """Delete a search space and all its documents."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.delete(
            f"{SURFSENSE_BASE}/api/v1/searchspaces/{search_space_id}",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        return {"type": "search_space_deleted", "id": search_space_id, "status": "deleted"}


# ===========================================================================
# THREAD / CHAT TOOLS
# ===========================================================================


async def tool_query(query: str, search_space_id: int, thread_id: str | None = None) -> dict:
    """Query the knowledge base. Creates a thread if needed, streams response."""
    async with httpx.AsyncClient(timeout=120) as client:
        token = await get_token(client)
        headers = auth_headers(token)

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

        dashboard_data = None
        try:
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", full_response, re.DOTALL)
            if json_match:
                dashboard_data = json.loads(json_match.group(1))
            else:
                dashboard_data = json.loads(full_response)
        except (json.JSONDecodeError, AttributeError):
            dashboard_data = {"type": "summary", "content": full_response, "query": query}

        return {
            "type": "query_result",
            "thread_id": thread_id,
            "search_space_id": search_space_id,
            "query": query,
            "dashboard": dashboard_data,
        }


async def tool_list_threads(search_space_id: int | None = None) -> dict:
    """List conversation threads."""
    params: dict[str, Any] = {}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id

    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/threads",
            headers=auth_headers(token),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        threads = data if isinstance(data, list) else data.get("items", [])
        return {
            "type": "thread_list",
            "threads": [
                {
                    "id": t["id"],
                    "title": t.get("title"),
                    "search_space_id": t.get("search_space_id"),
                    "created_at": t.get("created_at"),
                }
                for t in threads
            ],
        }


async def tool_get_thread(thread_id: int) -> dict:
    """Get detail of a specific thread."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/threads/{thread_id}",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        t = resp.json()
        return {
            "type": "thread_detail",
            "id": t["id"],
            "title": t.get("title"),
            "search_space_id": t.get("search_space_id"),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
        }


async def tool_delete_thread(thread_id: int) -> dict:
    """Delete a conversation thread."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.delete(
            f"{SURFSENSE_BASE}/api/v1/threads/{thread_id}",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        return {"type": "thread_deleted", "id": thread_id, "status": "deleted"}


async def tool_thread_history(thread_id: int) -> dict:
    """Get all messages in a thread."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/threads/{thread_id}/messages",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        data = resp.json()
        messages = data if isinstance(data, list) else data.get("items", [])
        return {
            "type": "thread_history",
            "thread_id": thread_id,
            "messages": [
                {
                    "id": m.get("id"),
                    "role": m.get("role"),
                    "content": m.get("content", "")[:500],
                    "created_at": m.get("created_at"),
                }
                for m in messages
            ],
        }


# ===========================================================================
# REPORT TOOLS
# ===========================================================================


async def tool_list_reports(search_space_id: int | None = None) -> dict:
    """List generated reports."""
    params: dict[str, Any] = {}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id

    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/reports",
            headers=auth_headers(token),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        reports = data if isinstance(data, list) else data.get("items", [])
        return {
            "type": "report_list",
            "reports": [
                {
                    "id": r["id"],
                    "title": r.get("title"),
                    "created_at": r.get("created_at"),
                }
                for r in reports
            ],
        }


async def tool_get_report(report_id: int) -> dict:
    """Get the content of a report."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/reports/{report_id}/content",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "type": "report_content",
            "id": report_id,
            "content": data.get("content", data),
        }


async def tool_export_report(report_id: int) -> dict:
    """Export a report (returns download URL or binary info)."""
    async with httpx.AsyncClient(timeout=60) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/reports/{report_id}/export",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            return {"type": "report_export", "id": report_id, "data": resp.json()}
        return {
            "type": "report_export",
            "id": report_id,
            "content_type": content_type,
            "size_bytes": len(resp.content),
            "message": "Report exported. Binary content available via direct download.",
        }


async def tool_delete_report(report_id: int) -> dict:
    """Delete a report."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.delete(
            f"{SURFSENSE_BASE}/api/v1/reports/{report_id}",
            headers=auth_headers(token),
        )
        resp.raise_for_status()
        return {"type": "report_deleted", "id": report_id, "status": "deleted"}


# ===========================================================================
# NOTES TOOL
# ===========================================================================


async def tool_create_note(search_space_id: int, content: str) -> dict:
    """Create a note in a search space."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.post(
            f"{SURFSENSE_BASE}/api/v1/search-spaces/{search_space_id}/notes",
            headers=auth_headers(token),
            json={"content": content},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "type": "note_created",
            "id": data.get("id"),
            "search_space_id": search_space_id,
        }


# ===========================================================================
# LOGS TOOL
# ===========================================================================


async def tool_get_logs(search_space_id: int | None = None, limit: int = 50) -> dict:
    """Get audit logs."""
    params: dict[str, Any] = {"page_size": limit}
    if search_space_id is not None:
        params["search_space_id"] = search_space_id

    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        resp = await client.get(
            f"{SURFSENSE_BASE}/api/v1/logs",
            headers=auth_headers(token),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        logs = data if isinstance(data, list) else data.get("items", [])
        return {
            "type": "audit_logs",
            "logs": [
                {
                    "id": l.get("id"),
                    "action": l.get("action"),
                    "details": l.get("details"),
                    "created_at": l.get("created_at"),
                }
                for l in logs[:limit]
            ],
        }




# ===========================================================================
# MCP TOOL DEFINITIONS
# ===========================================================================

TOOL_DEFINITIONS = [
    # --- DOCUMENTS ---
    {
        "name": "surfsense_upload",
        "description": "Upload a document (PDF, Excel, Word, CSV, etc.) to SurfSense for indexing. Returns document_ids and queued status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file"},
                "search_space_id": {"type": "integer", "description": "Target search space ID"},
            },
            "required": ["file_path", "search_space_id"],
        },
    },
    {
        "name": "surfsense_list_documents",
        "description": "List all indexed documents in a search space with their status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Search space ID"},
                "page": {"type": "integer", "description": "Page number (0-based)", "default": 0},
                "page_size": {"type": "integer", "description": "Items per page", "default": 50},
            },
            "required": ["search_space_id"],
        },
    },
    {
        "name": "surfsense_get_document",
        "description": "Get full detail of a specific document including content and metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "integer", "description": "Document ID"},
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "surfsense_delete_document",
        "description": "Permanently delete a document from the knowledge base.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "integer", "description": "Document ID to delete"},
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "surfsense_update_document",
        "description": "Update a document's title or metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "integer", "description": "Document ID"},
                "title": {"type": "string", "description": "New title"},
                "document_metadata": {"type": "object", "description": "Updated metadata dict"},
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "surfsense_document_status",
        "description": "Batch status check for documents. Returns processing state (processing/ready/error) for each ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Search space ID"},
                "document_ids": {"type": "string", "description": "Comma-separated document IDs (e.g. '1,2,3')"},
            },
            "required": ["search_space_id", "document_ids"],
        },
    },
    {
        "name": "surfsense_search_documents",
        "description": "Search documents by title substring. Case-insensitive partial match.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Search term (matches title substring)"},
                "search_space_id": {"type": "integer", "description": "Optional: limit to specific space"},
                "page_size": {"type": "integer", "description": "Max results", "default": 50},
            },
            "required": ["title"],
        },
    },
    {
        "name": "surfsense_type_counts",
        "description": "Get document counts grouped by type (PDF, EXTENSION, FILE, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Optional: limit to specific space"},
            },
            "required": [],
        },
    },
    {
        "name": "surfsense_extract_tables",
        "description": "Extract all tables, metrics, and structured data from a specific document for dashboard rendering.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "integer", "description": "Document ID"},
                "search_space_id": {"type": "integer", "description": "Search space ID"},
            },
            "required": ["doc_id", "search_space_id"],
        },
    },
    # --- SEARCH SPACES ---
    {
        "name": "surfsense_list_spaces",
        "description": "List all available search spaces (knowledge bases).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "surfsense_create_space",
        "description": "Create a new search space (knowledge base) for organizing documents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name for the search space"},
                "description": {"type": "string", "description": "Optional description"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "surfsense_get_space",
        "description": "Get detail of a specific search space.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Search space ID"},
            },
            "required": ["search_space_id"],
        },
    },
    {
        "name": "surfsense_update_space",
        "description": "Update a search space's name or description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Search space ID"},
                "name": {"type": "string", "description": "New name"},
                "description": {"type": "string", "description": "New description"},
            },
            "required": ["search_space_id"],
        },
    },
    {
        "name": "surfsense_delete_space",
        "description": "Delete a search space and ALL its documents. This action is irreversible.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Search space ID to delete"},
            },
            "required": ["search_space_id"],
        },
    },
    # --- THREADS / CHAT ---
    {
        "name": "surfsense_query",
        "description": "Query the knowledge base with natural language. Returns structured JSON for dashboard rendering. Creates a new thread or continues an existing one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query"},
                "search_space_id": {"type": "integer", "description": "Search space ID"},
                "thread_id": {"type": "string", "description": "Optional existing thread ID to continue conversation"},
            },
            "required": ["query", "search_space_id"],
        },
    },
    {
        "name": "surfsense_list_threads",
        "description": "List conversation threads, optionally filtered by search space.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Optional: filter by search space"},
            },
            "required": [],
        },
    },
    {
        "name": "surfsense_get_thread",
        "description": "Get detail of a specific conversation thread.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "integer", "description": "Thread ID"},
            },
            "required": ["thread_id"],
        },
    },
    {
        "name": "surfsense_delete_thread",
        "description": "Delete a conversation thread and all its messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "integer", "description": "Thread ID to delete"},
            },
            "required": ["thread_id"],
        },
    },
    {
        "name": "surfsense_thread_history",
        "description": "Get all messages in a conversation thread.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "integer", "description": "Thread ID"},
            },
            "required": ["thread_id"],
        },
    },
    # --- REPORTS ---
    {
        "name": "surfsense_list_reports",
        "description": "List generated reports, optionally filtered by search space.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Optional: filter by search space"},
            },
            "required": [],
        },
    },
    {
        "name": "surfsense_get_report",
        "description": "Get the content of a generated report.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "report_id": {"type": "integer", "description": "Report ID"},
            },
            "required": ["report_id"],
        },
    },
    {
        "name": "surfsense_export_report",
        "description": "Export a report for download (PDF or other format).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "report_id": {"type": "integer", "description": "Report ID"},
            },
            "required": ["report_id"],
        },
    },
    {
        "name": "surfsense_delete_report",
        "description": "Delete a generated report.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "report_id": {"type": "integer", "description": "Report ID to delete"},
            },
            "required": ["report_id"],
        },
    },
    # --- NOTES ---
    {
        "name": "surfsense_create_note",
        "description": "Create a note attached to a search space. Useful for adding annotations or context about documents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Search space ID"},
                "content": {"type": "string", "description": "Note content (text or markdown)"},
            },
            "required": ["search_space_id", "content"],
        },
    },
    # --- LOGS ---
    {
        "name": "surfsense_get_logs",
        "description": "Get audit logs for activity tracking. Shows who did what and when.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_space_id": {"type": "integer", "description": "Optional: filter by search space"},
                "limit": {"type": "integer", "description": "Max entries to return", "default": 50},
            },
            "required": [],
        },
    },
]


# ===========================================================================
# TOOL DISPATCHER
# ===========================================================================


async def dispatch_tool(name: str, args: dict) -> Any:
    """Route MCP tool calls to their implementations."""
    match name:
        # Documents
        case "surfsense_upload":
            return await tool_upload(**args)
        case "surfsense_list_documents":
            return await tool_list_documents(**args)
        case "surfsense_get_document":
            return await tool_get_document(**args)
        case "surfsense_delete_document":
            return await tool_delete_document(**args)
        case "surfsense_update_document":
            return await tool_update_document(**args)
        case "surfsense_document_status":
            return await tool_document_status(**args)
        case "surfsense_search_documents":
            return await tool_search_documents(**args)
        case "surfsense_type_counts":
            return await tool_type_counts(**args)
        case "surfsense_extract_tables":
            return await tool_extract_tables(**args)
        # Search Spaces
        case "surfsense_list_spaces":
            return await tool_list_spaces()
        case "surfsense_create_space":
            return await tool_create_space(**args)
        case "surfsense_get_space":
            return await tool_get_space(**args)
        case "surfsense_update_space":
            return await tool_update_space(**args)
        case "surfsense_delete_space":
            return await tool_delete_space(**args)
        # Threads / Chat
        case "surfsense_query":
            return await tool_query(**args)
        case "surfsense_list_threads":
            return await tool_list_threads(**args)
        case "surfsense_get_thread":
            return await tool_get_thread(**args)
        case "surfsense_delete_thread":
            return await tool_delete_thread(**args)
        case "surfsense_thread_history":
            return await tool_thread_history(**args)
        # Reports
        case "surfsense_list_reports":
            return await tool_list_reports(**args)
        case "surfsense_get_report":
            return await tool_get_report(**args)
        case "surfsense_export_report":
            return await tool_export_report(**args)
        case "surfsense_delete_report":
            return await tool_delete_report(**args)
        # Notes
        case "surfsense_create_note":
            return await tool_create_note(**args)
        # Logs
        case "surfsense_get_logs":
            return await tool_get_logs(**args)
        case _:
            raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")


# ===========================================================================
# MCP JSON-RPC ENDPOINT
# ===========================================================================


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """Handle MCP JSON-RPC requests from Hermes."""
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    req_id = body.get("id")

    logger.info("MCP request: %s", method)

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "documenter-mcp", "version": "0.2.0"},
            }

        elif method == "tools/list":
            result = {"tools": TOOL_DEFINITIONS}

        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            logger.info("Tool call: %s(%s)", tool_name, list(tool_args.keys()))
            data = await dispatch_tool(tool_name, tool_args)
            result = {
                "content": [
                    {"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}
                ]
            }

        elif method == "notifications/initialized":
            return JSONResponse(content=None, status_code=204)

        else:
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )

        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": result})

    except HTTPException as e:
        logger.error("Tool error: %s", e.detail)
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": e.detail},
            },
            status_code=200,
        )
    except Exception as e:
        logger.exception("Unexpected error in MCP handler")
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            },
            status_code=200,
        )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "documenter-mcp", "tools": len(TOOL_DEFINITIONS)}


@app.get("/tools")
async def list_tools() -> dict:
    """Debug endpoint — list available tools with descriptions."""
    return {
        "total": len(TOOL_DEFINITIONS),
        "tools": [{"name": t["name"], "description": t["description"]} for t in TOOL_DEFINITIONS],
    }


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    logger.info("Starting DocuMentor MCP Wrapper v0.2.0 on port %s", MCP_PORT)
    logger.info("SurfSense backend: %s", SURFSENSE_BASE)
    logger.info("Registered %d tools", len(TOOL_DEFINITIONS))
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="info")
