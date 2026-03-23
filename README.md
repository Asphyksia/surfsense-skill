# hermes-surfsense-skill

Hermes Agent skill for [SurfSense](https://github.com/MODSetter/SurfSense) — full RAG document intelligence via MCP.

**25 tools** for document management, knowledge base search, conversation threads, reports, notes, and audit logs.

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure environment
export SURFSENSE_BASE_URL=http://localhost:8929
export SURFSENSE_EMAIL=admin@example.com
export SURFSENSE_PASSWORD=your_password

# 3. Start MCP server
python mcp_server.py

# 4. Add to ~/.hermes/config.yaml
# mcp_servers:
#   surfsense:
#     url: "http://localhost:8000/mcp"
#     timeout: 120

# 5. Restart Hermes — 25 tools auto-discovered as mcp_surfsense_*
```

## What's Included

| File | Description |
|------|-------------|
| `SKILL.md` | Full documentation with all 25 tools, usage patterns, config |
| `mcp_server.py` | MCP server wrapping SurfSense's REST API |
| `requirements.txt` | Python dependencies |
| `hermes-config-snippet.yaml` | Copy-paste config for Hermes |

## Requirements

- SurfSense instance running (Docker recommended)
- Python 3.11+
- Hermes Agent with native-mcp skill (included by default)

See [SKILL.md](SKILL.md) for complete documentation.

## License

MIT
