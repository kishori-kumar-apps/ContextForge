# ContextForge operations

## Production configuration

| Variable | Default | Purpose |
|---|---:|---|
| `CONTEXTFORGE_DB` | `data/contextforge.db` | SQLite database path; use persistent storage in production. |
| `CONTEXTFORGE_BACKUP_DIR` | `data/backups` | Integrity-checked backup destination. |
| `CONTEXTFORGE_BACKUP_KEEP` | `5` | Number of newest backups retained. |
| `CONTEXTFORGE_BUSY_TIMEOUT_MS` | `5000` | SQLite wait before one lock attempt fails. |
| `CONTEXTFORGE_WRITE_RETRIES` | `5` | Bounded write attempts; maximum accepted value is 10. |
| `CONTEXTFORGE_LOG_LEVEL` | `INFO` | Python logging level. |
| `CONTEXTFORGE_STORAGE_MODE` | `self_hosted` | `self_hosted` or tenant-isolated `hosted`. |
| `CONTEXTFORGE_KEY_PEPPER` | empty | Required hosted-mode HMAC secret; keep only in Space secrets. |

Malformed values fail startup intentionally instead of silently selecting an
unknown production configuration.

## Health and monitoring

Call the MCP tool `health_check`. A healthy response verifies:

- database reachability;
- SQLite `quick_check` integrity;
- expected schema metadata;
- database size and query latency.

Alert on `degraded` or `unhealthy`. Tool failures return stable codes:
`validation_error`, `not_found`, `store_unavailable`, or `internal_error`.
Unexpected failures include an incident ID in the client response and full
details in server logs without exposing a traceback to clients.

## Backups and restore

`backup_database` uses SQLite's online backup API, checks the new database, and
prunes files beyond `CONTEXTFORGE_BACKUP_KEEP`. Put the backup directory on
persistent storage and copy backups off-Space according to your recovery policy.

Restore while the Space is stopped:

1. Preserve the current database and its `-wal` and `-shm` files.
2. Copy a verified backup to the configured `CONTEXTFORGE_DB` path.
3. Restart the Space.
4. Call `health_check` and confirm `integrity: ok`.

Periodically perform a restore drill; a backup that has never been restored is
not a verified recovery process.

## Capacity and concurrency

Writes are serialized inside one process and SQLite uses WAL mode. External lock
contention receives exponential retries and then a bounded `store_unavailable`
response instead of hanging indefinitely. This design fits a single Gradio
Space replica. Move to a managed database before using multiple replicas or
high sustained write throughput.

Input limits protect memory and query cost: titles are 300 characters, bodies
50,000 characters, sources 100 characters, search queries 1,000 characters and
20 words, evidence 100 items, and metadata 100,000 encoded bytes.

## Security boundary

Hosted mode resolves the tenant from an `Authorization: Bearer cf_live_...`
header. MCP tools never accept `tenant_id`, and every record operation is scoped
by the authenticated tenant. Only an HMAC-SHA256 digest is stored; keep the
pepper in Hugging Face Space secrets and revoke exposed consumer keys.

Self-hosted mode intentionally uses one implicit `local` tenant without
authentication. Do not expose it to untrusted networks. SQLite isolation is
enforced by the repository, not SQLite row-level security. Move to PostgreSQL
before multiple replicas or high sustained concurrency.

