from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

SCHEMA_VERSION = 2
ALLOWED_KINDS = {"decision", "work_packet", "implementation"}
ALLOWED_STATUSES = {"active", "ready", "in_progress", "complete", "superseded", "blocked"}
MAX_TITLE_LENGTH = 300
MAX_BODY_LENGTH = 50_000
MAX_SOURCE_LENGTH = 100
MAX_EVIDENCE_ITEMS = 100
MAX_EVIDENCE_LENGTH = 2_000
MAX_METADATA_BYTES = 100_000
MAX_SEARCH_LENGTH = 1_000
MAX_SEARCH_WORDS = 20
T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StoreError(RuntimeError):
    """Base error safe for conversion into an MCP error response."""


class ValidationError(StoreError):
    """Input failed a documented ContextForge constraint."""


class NotFoundError(StoreError):
    """Requested record does not exist."""


class StoreUnavailableError(StoreError):
    """SQLite remained unavailable after bounded retries."""


class AuthenticationError(StoreError):
    """A consumer credential is missing, invalid, expired, or revoked."""


class ContextStore:
    """Resilient SQLite-backed project memory shared by UI and MCP tools."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000,
                 write_retries: int = 5, retry_base_seconds: float = 0.05) -> None:
        self.path = str(Path(path))
        self.busy_timeout_ms = max(100, int(busy_timeout_ms))
        self.write_retries = max(1, min(int(write_retries), 10))
        self.retry_base_seconds = max(0.001, float(retry_base_seconds))
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_ms / 1000)
        else:
            connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        def operation(db: sqlite3.Connection) -> None:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    body TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '[]', metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'local'
                );
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    key_hash TEXT NOT NULL, key_prefix TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT,
                    FOREIGN KEY(tenant_id) REFERENCES tenants(id)
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(records)")}
            if "tenant_id" not in columns:
                db.execute("ALTER TABLE records ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
            db.execute("CREATE INDEX IF NOT EXISTS idx_records_tenant_kind "
                       "ON records(tenant_id, kind)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_records_tenant_updated "
                       "ON records(tenant_id, updated_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id)")
            db.execute(
                "INSERT OR IGNORE INTO tenants(id, name, active, created_at) VALUES(?, ?, 1, ?)",
                ("local", "Local", _now()),
            )
            db.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

        self._write(operation)

    def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        last_error: sqlite3.OperationalError | None = None
        with self._lock:
            for attempt in range(self.write_retries):
                try:
                    with closing(self._connect()) as db, db:
                        return operation(db)
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise StoreUnavailableError(f"database operation failed: {exc}") from exc
                    last_error = exc
                    if attempt + 1 < self.write_retries:
                        time.sleep(self.retry_base_seconds * (2**attempt))
            raise StoreUnavailableError("database remained busy after bounded retries") from last_error

    def add(self, kind: str, title: str, body: str, source: str, status: str = "active",
            evidence: list[str] | None = None, metadata: dict[str, Any] | None = None,
            *, tenant_id: str = "local") -> dict[str, Any]:
        tenant_id = self._validate_tenant_id(tenant_id)
        clean = self._validate_record(kind, title, body, source, status, evidence, metadata)
        record_id, timestamp = str(uuid.uuid4()), _now()

        def operation(db: sqlite3.Connection) -> None:
            db.execute("INSERT INTO records "
                       "(id, kind, title, body, source, status, evidence, metadata, created_at, updated_at, tenant_id) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                record_id, clean["kind"], clean["title"], clean["body"], clean["source"],
                clean["status"], json.dumps(clean["evidence"], ensure_ascii=False),
                json.dumps(clean["metadata"], ensure_ascii=False), timestamp, timestamp, tenant_id,
            ))

        self._write(operation)
        return self.get(record_id, tenant_id=tenant_id)

    def get(self, record_id: str, *, tenant_id: str = "local") -> dict[str, Any]:
        self._validate_uuid(record_id)
        tenant_id = self._validate_tenant_id(tenant_id)
        try:
            with closing(self._connect(readonly=True)) as db:
                row = db.execute("SELECT * FROM records WHERE id = ? AND tenant_id = ?",
                                 (record_id, tenant_id)).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"database read failed: {exc}") from exc
        if row is None:
            raise NotFoundError(f"record not found: {record_id}")
        return self._decode(row)

    def list(self, kind: str | None = None, limit: int = 50, *,
             tenant_id: str = "local") -> list[dict[str, Any]]:
        tenant_id = self._validate_tenant_id(tenant_id)
        limit = self._bounded_limit(limit, 200)
        if kind and kind not in ALLOWED_KINDS:
            raise ValidationError(f"unsupported record kind: {kind}")
        query, params = "SELECT * FROM records WHERE tenant_id = ?", [tenant_id]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        try:
            with closing(self._connect(readonly=True)) as db:
                return [self._decode(row) for row in db.execute(query, params).fetchall()]
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise StoreUnavailableError(f"database read failed: {exc}") from exc

    def search(self, query: str, limit: int = 20, *,
               tenant_id: str = "local") -> list[dict[str, Any]]:
        tenant_id = self._validate_tenant_id(tenant_id)
        query = str(query or "").strip()
        if len(query) > MAX_SEARCH_LENGTH:
            raise ValidationError(f"query exceeds {MAX_SEARCH_LENGTH} characters")
        words = [word for word in query.split() if word]
        if len(words) > MAX_SEARCH_WORDS:
            raise ValidationError(f"query exceeds {MAX_SEARCH_WORDS} words")
        if not words:
            return self.list(limit=self._bounded_limit(limit, 100), tenant_id=tenant_id)
        clauses, params = [], [tenant_id]
        for word in words:
            clauses.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR evidence LIKE ? ESCAPE '\\')")
            escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            token = f"%{escaped}%"
            params.extend([token, token, token])
        params.append(self._bounded_limit(limit, 100))
        sql = "SELECT * FROM records WHERE tenant_id = ? AND " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        try:
            with closing(self._connect(readonly=True)) as db:
                return [self._decode(row) for row in db.execute(sql, params).fetchall()]
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise StoreUnavailableError(f"database search failed: {exc}") from exc

    def update_status(self, record_id: str, status: str, *,
                      tenant_id: str = "local") -> dict[str, Any]:
        self._validate_uuid(record_id)
        tenant_id = self._validate_tenant_id(tenant_id)
        if status not in ALLOWED_STATUSES:
            raise ValidationError(f"unsupported status: {status}")
        timestamp = _now()

        def operation(db: sqlite3.Connection) -> None:
            cursor = db.execute("UPDATE records SET status = ?, updated_at = ? "
                                "WHERE id = ? AND tenant_id = ?",
                                (status, timestamp, record_id, tenant_id))
            if cursor.rowcount == 0:
                raise NotFoundError(f"record not found: {record_id}")

        self._write(operation)
        return self.get(record_id, tenant_id=tenant_id)

    def summary(self, *, tenant_id: str = "local") -> dict[str, Any]:
        tenant_id = self._validate_tenant_id(tenant_id)
        try:
            with closing(self._connect(readonly=True)) as db:
                counts = {row["kind"]: row["count"] for row in db.execute(
                    "SELECT kind, COUNT(*) AS count FROM records WHERE tenant_id = ? GROUP BY kind",
                    (tenant_id,)).fetchall()}
                latest = db.execute("SELECT MAX(updated_at) AS value FROM records WHERE tenant_id = ?",
                                    (tenant_id,)).fetchone()["value"]
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"database summary failed: {exc}") from exc
        return {"counts": counts, "total": sum(counts.values()), "last_updated": latest}

    def create_tenant(self, name: str) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name or len(name) > 200:
            raise ValidationError("tenant name must contain 1 to 200 characters")
        tenant_id, timestamp = str(uuid.uuid4()), _now()

        def operation(db: sqlite3.Connection) -> None:
            try:
                db.execute("INSERT INTO tenants(id, name, active, created_at) VALUES(?, ?, 1, ?)",
                           (tenant_id, name, timestamp))
            except sqlite3.IntegrityError as exc:
                raise ValidationError(f"tenant already exists: {name}") from exc

        self._write(operation)
        return {"id": tenant_id, "name": name, "active": True, "created_at": timestamp}

    def issue_api_key(self, tenant_id: str, pepper: str,
                      *, expires_at: str | None = None) -> dict[str, Any]:
        tenant_id = self._validate_tenant_id(tenant_id)
        if not pepper:
            raise ValidationError("CONTEXTFORGE_KEY_PEPPER is required to issue API keys")
        # Hex is intentionally delimiter-safe because the public credential uses
        # underscores to separate cf_live, key_id, and the secret.
        key_id = secrets.token_hex(9)
        secret = secrets.token_urlsafe(32)
        api_key = f"cf_live_{key_id}_{secret}"
        key_hash, timestamp = self._key_digest(api_key, pepper), _now()

        def operation(db: sqlite3.Connection) -> None:
            tenant = db.execute("SELECT active FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            if tenant is None:
                raise NotFoundError(f"tenant not found: {tenant_id}")
            if not tenant["active"]:
                raise ValidationError("tenant is inactive")
            db.execute(
                "INSERT INTO api_keys(key_id, tenant_id, key_hash, key_prefix, created_at, expires_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (key_id, tenant_id, key_hash, f"cf_live_{key_id}", timestamp, expires_at),
            )

        self._write(operation)
        return {"api_key": api_key, "key_id": key_id, "tenant_id": tenant_id,
                "created_at": timestamp, "expires_at": expires_at}

    def authenticate(self, authorization: str | None, pepper: str) -> str:
        if not pepper:
            raise AuthenticationError("hosted authentication is not configured")
        scheme, separator, api_key = str(authorization or "").strip().partition(" ")
        if not separator or scheme.lower() != "bearer" or not api_key:
            raise AuthenticationError("missing or invalid Authorization bearer token")
        parts = api_key.split("_", 3)
        if len(parts) != 4 or parts[:2] != ["cf", "live"]:
            raise AuthenticationError("invalid ContextForge API key")
        try:
            with closing(self._connect(readonly=True)) as db:
                row = db.execute(
                    "SELECT k.*, t.active AS tenant_active FROM api_keys k "
                    "JOIN tenants t ON t.id = k.tenant_id WHERE k.key_id = ?", (parts[2],),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StoreUnavailableError(f"database authentication read failed: {exc}") from exc
        digest = self._key_digest(api_key, pepper)
        if row is None or not hmac.compare_digest(digest, row["key_hash"]):
            raise AuthenticationError("invalid ContextForge API key")
        if row["revoked_at"] is not None or not row["tenant_active"]:
            raise AuthenticationError("ContextForge API key is revoked")
        if row["expires_at"] and row["expires_at"] <= _now():
            raise AuthenticationError("ContextForge API key is expired")
        return str(row["tenant_id"])

    def revoke_api_key(self, key_id: str) -> None:
        def operation(db: sqlite3.Connection) -> None:
            cursor = db.execute("UPDATE api_keys SET revoked_at = ? "
                                "WHERE key_id = ? AND revoked_at IS NULL", (_now(), str(key_id)))
            if cursor.rowcount == 0:
                raise NotFoundError(f"active API key not found: {key_id}")
        self._write(operation)

    def health(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            with closing(self._connect(readonly=True)) as db:
                integrity = db.execute("PRAGMA quick_check").fetchone()[0]
                version_row = db.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
            healthy = integrity == "ok" and version_row is not None
            return {
                "status": "healthy" if healthy else "degraded", "database": "reachable",
                "integrity": integrity, "schema_version": int(version_row[0]) if version_row else None,
                "database_bytes": Path(self.path).stat().st_size,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2), "checked_at": _now(),
            }
        except (OSError, sqlite3.Error, ValueError) as exc:
            return {"status": "unhealthy", "database": "unavailable", "error": str(exc), "checked_at": _now()}

    def backup(self, directory: str | Path, *, keep: int = 5) -> dict[str, Any]:
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = target_dir / f"contextforge-{stamp}.db"
        try:
            with self._lock, closing(self._connect(readonly=True)) as source:
                with closing(sqlite3.connect(target)) as destination:
                    source.backup(destination)
                    integrity = destination.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                target.unlink(missing_ok=True)
                raise StoreUnavailableError(f"backup integrity check failed: {integrity}")
            backups = sorted(target_dir.glob("contextforge-*.db"), key=lambda item: item.stat().st_mtime,
                             reverse=True)
            for expired in backups[max(1, int(keep)):]:
                expired.unlink(missing_ok=True)
            return {"path": str(target), "bytes": target.stat().st_size, "integrity": integrity,
                    "created_at": _now()}
        except (OSError, sqlite3.Error) as exc:
            target.unlink(missing_ok=True)
            raise StoreUnavailableError(f"backup failed: {exc}") from exc

    @staticmethod
    def _validate_record(kind: str, title: str, body: str, source: str, status: str,
                         evidence: list[str] | None,
                         metadata: dict[str, Any] | None) -> dict[str, Any]:
        if kind not in ALLOWED_KINDS:
            raise ValidationError(f"unsupported record kind: {kind}")
        if status not in ALLOWED_STATUSES:
            raise ValidationError(f"unsupported status: {status}")
        title, body, source = str(title or "").strip(), str(body or "").strip(), str(source or "user").strip()
        if not title or not body:
            raise ValidationError("title and body are required")
        if len(title) > MAX_TITLE_LENGTH:
            raise ValidationError(f"title exceeds {MAX_TITLE_LENGTH} characters")
        if len(body) > MAX_BODY_LENGTH:
            raise ValidationError(f"body exceeds {MAX_BODY_LENGTH} characters")
        if len(source) > MAX_SOURCE_LENGTH:
            raise ValidationError(f"source exceeds {MAX_SOURCE_LENGTH} characters")
        evidence = list(evidence or [])
        if len(evidence) > MAX_EVIDENCE_ITEMS:
            raise ValidationError(f"evidence exceeds {MAX_EVIDENCE_ITEMS} items")
        evidence = [str(item).strip() for item in evidence if str(item).strip()]
        if any(len(item) > MAX_EVIDENCE_LENGTH for item in evidence):
            raise ValidationError(f"an evidence item exceeds {MAX_EVIDENCE_LENGTH} characters")
        metadata = dict(metadata or {})
        try:
            encoded_metadata = json.dumps(metadata, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValidationError("metadata must be JSON serializable") from exc
        if len(encoded_metadata.encode("utf-8")) > MAX_METADATA_BYTES:
            raise ValidationError(f"metadata exceeds {MAX_METADATA_BYTES} bytes")
        return {"kind": kind, "title": title, "body": body, "source": source or "user",
                "status": status, "evidence": evidence, "metadata": metadata}

    @staticmethod
    def _bounded_limit(limit: int, maximum: int) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValidationError("limit must be an integer") from exc
        if value < 1 or value > maximum:
            raise ValidationError(f"limit must be between 1 and {maximum}")
        return value

    @staticmethod
    def _validate_uuid(record_id: str) -> None:
        try:
            uuid.UUID(str(record_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("record_id must be a valid UUID") from exc

    @staticmethod
    def _validate_tenant_id(tenant_id: str) -> str:
        tenant_id = str(tenant_id or "").strip()
        if not tenant_id or len(tenant_id) > 100:
            raise ValidationError("tenant_id must contain 1 to 100 characters")
        return tenant_id

    @staticmethod
    def _key_digest(api_key: str, pepper: str) -> str:
        return hmac.new(pepper.encode("utf-8"), api_key.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["evidence"], result["metadata"] = json.loads(result["evidence"]), json.loads(result["metadata"])
        return result
