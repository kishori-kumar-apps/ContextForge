---
title: ContextForge
emoji: 🐢
colorFrom: yellow
colorTo: blue
sdk: gradio
sdk_version: 6.26.0
python_version: '3.12'
app_file: app.py
pinned: false
license: apache-2.0
short_description: Shared project memory for Claude and Codex agents.
---

# ContextForge

**Tenant-isolated MCP project memory for Claude and Codex agents.**

ContextForge is a Python and Gradio MCP server that enables compatible AI hosts
to store, search, and exchange architectural decisions, implementation notes,
and structured work packets. It runs locally or on Hugging Face Spaces without
requiring Docker.

[Try the Hugging Face Space](https://huggingface.co/spaces/kkishor191/ContextForge)

## Why it exists

AI coding sessions often lose important decisions when a conversation ends or
work moves between hosts. ContextForge gives Claude, Codex, and other MCP
clients a shared source of durable project knowledge.

```text
Claude / Codex
      │  Streamable HTTP MCP
      ▼
ContextForge (Gradio)
      │  trusted tenant context
      ▼
SQLite project memory
```

## MCP tools

- `record_decision`, `list_decisions`, and `search_project`
- `create_work_packet` and `get_work_packet`
- `record_implementation` and `update_record_status`
- `project_summary`, `health_check`, and `backup_database`

## Persistence modes

- **Self-hosted:** local SQLite, an implicit `local` tenant, and no API key.
- **Hosted:** shared SQLite with API-key authentication and tenant-scoped data.

Hosted keys use `cf_live_<key-id>_<secret>`. ContextForge stores only an
HMAC-SHA256 digest and resolves the tenant internally. MCP tools never accept a
caller-controlled `tenant_id`.

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Connect Claude or Codex to:

```text
http://localhost:7860/gradio_api/mcp/
```

Then try:

```text
Use ContextForge to record our decision to use SQLite for the MVP.
Search ContextForge for decisions about persistence and summarize them.
Create a work packet for migrating the storage adapter to PostgreSQL.
```

See [CONNECTING.md](CONNECTING.md) for client and hosted-authentication examples.

## Hosted configuration

Mount durable storage at `/data` and configure:

```text
CONTEXTFORGE_STORAGE_MODE=hosted
CONTEXTFORGE_DB=/data/contextforge.db
CONTEXTFORGE_BACKUP_DIR=/data/backups
CONTEXTFORGE_KEY_PEPPER=<secure-random-secret>
```

Provision consumers with:

```powershell
python admin.py create-tenant --name "Acme"
python admin.py issue-key --tenant-id "TENANT_UUID"
python admin.py revoke-key --key-id "KEY_ID"
```

## Engineering highlights

- Native Gradio Streamable HTTP MCP server
- SQLite WAL mode, busy timeout, and bounded write retries
- Automatic schema migration and integrity-checked backups
- Structured, redacted errors with incident IDs
- Tenant-scoped record operations and revocable API keys
- Input-size and query-complexity limits
- Compatibility with a Docker-free Hugging Face Space

SQLite tenancy is application-enforced and intended for a single-instance MVP.
PostgreSQL is the recommended next step for multiple replicas or sustained
write concurrency.

## Test

```powershell
python -m unittest discover -s tests -v
```

The suite covers record lifecycles, migration, backups, concurrent writes, lock
contention, validation, API-key authentication and revocation, MCP schema
discovery, and cross-tenant access denial.

## Stack

Python · Gradio · Model Context Protocol · SQLite · Hugging Face Spaces

See [OPERATIONS.md](OPERATIONS.md) for monitoring, recovery, capacity, and
security guidance.
