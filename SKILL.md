---
name: surfsense-rag
description: Full integration with SurfSense RAG backend. Gives Hermes 25 tools for document management, knowledge base search, conversation threads, reports, notes, and audit logs. Upload PDFs, spreadsheets, Word docs, images — ask questions in natural language, get structured data for dashboards.
version: 1.0.0
requires: SurfSense running (Docker recommended), Python 3.11+
author: Asphyksia
license: MIT
metadata:
  hermes:
    tags: [RAG, SurfSense, documents, knowledge-base, search, MCP]
    related_skills: [native-mcp]
---

# SurfSense RAG Skill

Connect Hermes Agent to a SurfSense instance for full document intelligence — upload, index, search, query, and manage documents through natural language.

## What This Gives You

25 MCP tools organized in 6 categories:

### Documents (9 tools)
| Tool | Description |
|------|-------------|
| `surfsense_upload` | Upload files (PDF, Excel, Word, CSV, images, etc.) for indexing |
| `surfsense_list_documents` | List all documents in a knowledge base |
| `surfsense_get_document` | Get full document detail and content |
| `surfsense_delete_document` | Delete a document |
| `surfsense_update_document` | Update title or metadata |
| `surfsense_document_status` | Batch check processing status (processing/ready/error) |
| `surfsense_search_documents` | Search documents by title |
| `surfsense_type_counts` | Count documents by type |
| `surfsense_extract_tables` | Extract structured tables and metrics for dashboard rendering |

### Knowledge Bases (5 tools)
| Tool | Description |
|------|-------------|
| `surfsense_list_spaces` | List all knowledge bases |
| `surfsense_create_space` | Create a new knowledge base |
| `surfsense_get_space` | Get knowledge base details |
| `surfsense_update_space` | Rename or update description |
| `surfsense_delete_space` | Delete a knowledge base and all its documents |

### Chat / RAG Queries (5 tools)
| Tool | Description |
|------|-------------|
| `surfsense_query` | Query the knowledge base in natural language — returns structured JSON |
| `surfsense_list_threads` | List conversation threads |
| `surfsense_get_thread` | Get thread details |
| `surfsense_delete_thread` | Delete a conversation thread |
| `surfsense_thread_history` | Get all messages in a thread |

### Reports (4 tools)
| Tool | Description |
|------|-------------|
| `surfsense_list_reports` | List generated reports |
| `surfsense_get_report` | Get report content |
| `surfsense_export_report` | Export report (PDF, etc.) |
| `surfsense_delete_report` | Delete a report |

### Notes (1 tool)
| Tool | Description |
|------|-------------|
| `surfsense_create_note` | Create a note attached to a knowledge base |

### Audit (1 tool)
| Tool | Description |
|------|-------------|
| `surfsense_get_logs` | Get activity logs for auditing |

## Setup

### 1. Start SurfSense

If you don't have SurfSense running, the fastest way is Docker:

```bash
git clone https://github.com/MODSetter/SurfSense
cd SurfSense/docker
cp .env.example .env
# Edit .env: set SECRET_KEY, OPENAI_API_KEY, etc.
docker compose up -d
```

SurfSense will be available at `http://localhost:8929`.

### 2. Start the MCP wrapper

```bash
cd /path/to/this/skill
pip install -r requirements.txt
python mcp_server.py
```

Or with environment variables:

```bash
SURFSENSE_BASE_URL=http://localhost:8929 \
SURFSENSE_EMAIL=admin@example.com \
SURFSENSE_PASSWORD=your_password \
MCP_PORT=8000 \
python mcp_server.py
```

The MCP server runs at `http://localhost:8000/mcp`.

### 3. Configure Hermes

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  surfsense:
    url: "http://localhost:8000/mcp"
    timeout: 120
    connect_timeout: 30
```

Restart Hermes. All 25 tools are auto-discovered and available as `mcp_surfsense_*`.

## Usage Patterns

### Upload and analyze a document
> "Upload /tmp/budget-2025.pdf to the Finance knowledge base and extract the key metrics"

Hermes will:
1. Call `surfsense_upload` with the file path
2. Poll `surfsense_document_status` until ready
3. Call `surfsense_extract_tables` for structured data
4. Present the results

### Ask questions about your documents
> "What was the total revenue in Q3 according to the annual report?"

Hermes calls `surfsense_query` which searches across all indexed documents and returns cited answers.

### Manage knowledge bases
> "Create a knowledge base called 'HR Policies' and upload all the PDFs in /tmp/hr-docs/"

Hermes calls `surfsense_create_space`, then `surfsense_upload` for each file.

### Check document status
> "Are all my uploaded documents indexed yet?"

Hermes calls `surfsense_document_status` with the relevant document IDs.

### Get audit trail
> "Show me the activity log for the Finance knowledge base"

Hermes calls `surfsense_get_logs` filtered by search space.

## Configuration

All configuration via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `SURFSENSE_BASE_URL` | `http://localhost:8929` | SurfSense backend URL |
| `SURFSENSE_EMAIL` | `admin@documenter.local` | Login email |
| `SURFSENSE_PASSWORD` | `admin` | Login password |
| `MCP_PORT` | `8000` | Port for this MCP server |

## Dashboard Integration

The `surfsense_query` and `surfsense_extract_tables` tools return structured JSON designed for dashboard rendering. The output format follows DOCSTEMPLATES schema:

```json
{
  "type": "pdf",
  "summary": "Executive summary...",
  "views": [
    { "type": "kpi", "title": "Total Revenue", "value": 1500000, "unit": "€" },
    { "type": "bar", "title": "Revenue by Quarter", "x_axis": "Quarter", "y_axis": "Revenue", "data": [...] },
    { "type": "table", "title": "Top Expenses", "headers": [...], "rows": [...] }
  ]
}
```

Supported view types: `kpi`, `table`, `bar`, `line`, `text`, `pie`, `area`, `metric_delta`.

## Supported File Formats

Via Docling (IBM's document parser):
- PDF, DOCX, PPTX, XLSX, CSV
- HTML, Markdown, AsciiDoc
- Images (PNG, JPG, TIFF, BMP) with OCR
- XML

## Notes

- Documents are stored and indexed locally — no data leaves your infrastructure
- Embeddings are generated locally using `sentence-transformers/all-MiniLM-L6-v2`
- The MCP server authenticates with SurfSense via JWT (auto-managed, cached)
- All tools are read-safe by default; destructive operations (delete) require explicit IDs
- Thread-based queries maintain conversation context for follow-up questions
