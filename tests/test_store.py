import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from contextforge import (AuthenticationError, ContextStore, NotFoundError,
                          StoreUnavailableError, ValidationError)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = ContextStore(self.root / "test.db", busy_timeout_ms=100,
                                  write_retries=2, retry_base_seconds=0.001)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_record_search_and_summary(self):
        record = self.store.add("decision", "Use Gradio SDK",
                                "Deploy without Docker and expose native MCP tools.",
                                "user", evidence=["README.md"])
        self.assertEqual(self.store.get(record["id"])["title"], "Use Gradio SDK")
        self.assertEqual(self.store.search("native MCP")[0]["id"], record["id"])
        self.assertEqual(self.store.summary(), {
            "counts": {"decision": 1}, "total": 1,
            "last_updated": record["updated_at"],
        })

    def test_empty_search_lists_latest(self):
        self.store.add("work_packet", "Build MVP", "Create tools", "Claude")
        self.assertEqual(len(self.store.search("")), 1)

    def test_wildcards_are_literal_not_unbounded_patterns(self):
        self.store.add("decision", "100% coverage", "Use literal underscore_value", "Codex")
        self.store.add("decision", "Unrelated", "ordinary text", "Claude")
        self.assertEqual(len(self.store.search("100%")), 1)
        self.assertEqual(len(self.store.search("underscore_value")), 1)

    def test_rejects_invalid_and_oversized_inputs(self):
        with self.assertRaises(ValidationError):
            self.store.add("unknown", "Title", "Body", "user")
        with self.assertRaises(ValidationError):
            self.store.add("decision", "", "Body", "user")
        with self.assertRaises(ValidationError):
            self.store.add("decision", "x" * 301, "Body", "user")
        with self.assertRaises(ValidationError):
            self.store.search("word " * 21)
        with self.assertRaises(ValidationError):
            self.store.list(limit=0)
        with self.assertRaises(ValidationError):
            self.store.get("not-a-uuid")

    def test_status_lifecycle_update(self):
        record = self.store.add("work_packet", "Ship", "Complete release", "Claude", status="ready")
        updated = self.store.update_status(record["id"], "in_progress")
        self.assertEqual(updated["status"], "in_progress")
        with self.assertRaises(ValidationError):
            self.store.update_status(record["id"], "invented")

    def test_health_reports_integrity_and_schema(self):
        health = self.store.health()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["integrity"], "ok")
        self.assertEqual(health["schema_version"], 2)
        self.assertGreaterEqual(health["database_bytes"], 1)

    def test_backup_is_valid_and_retention_is_bounded(self):
        self.store.add("decision", "Persist", "Back up this record", "user")
        backup_dir = self.root / "backups"
        for _ in range(3):
            result = self.store.backup(backup_dir, keep=2)
            self.assertEqual(result["integrity"], "ok")
        backups = list(backup_dir.glob("contextforge-*.db"))
        self.assertEqual(len(backups), 2)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1)

    def test_concurrent_writes_do_not_lose_records(self):
        def write(index):
            return self.store.add("implementation", f"Change {index}", "Completed", "Codex")

        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(write, range(30)))
        self.assertEqual(len({record["id"] for record in records}), 30)
        self.assertEqual(self.store.summary()["total"], 30)

    def test_tenants_are_isolated_for_every_record_operation(self):
        tenant_a = self.store.create_tenant("Tenant A")["id"]
        tenant_b = self.store.create_tenant("Tenant B")["id"]
        record = self.store.add("decision", "Private", "Only A can see this", "Codex",
                                tenant_id=tenant_a)
        self.assertEqual(self.store.summary(tenant_id=tenant_a)["total"], 1)
        self.assertEqual(self.store.summary(tenant_id=tenant_b)["total"], 0)
        self.assertEqual(self.store.search("Private", tenant_id=tenant_b), [])
        self.assertEqual(self.store.list(tenant_id=tenant_b), [])
        with self.assertRaises(NotFoundError):
            self.store.get(record["id"], tenant_id=tenant_b)
        with self.assertRaises(NotFoundError):
            self.store.update_status(record["id"], "complete", tenant_id=tenant_b)

    def test_api_key_authentication_and_revocation(self):
        tenant = self.store.create_tenant("Consumer")["id"]
        issued = self.store.issue_api_key(tenant, "test-pepper")
        self.assertEqual(self.store.authenticate(f"Bearer {issued['api_key']}", "test-pepper"), tenant)
        with self.assertRaises(AuthenticationError):
            self.store.authenticate(f"Bearer {issued['api_key']}", "wrong-pepper")
        with self.assertRaises(AuthenticationError):
            self.store.authenticate(None, "test-pepper")
        self.store.revoke_api_key(issued["key_id"])
        with self.assertRaises(AuthenticationError):
            self.store.authenticate(f"Bearer {issued['api_key']}", "test-pepper")

    def test_many_generated_api_keys_have_unambiguous_ids(self):
        tenant = self.store.create_tenant("Key Stress Consumer")["id"]
        for _ in range(50):
            issued = self.store.issue_api_key(tenant, "test-pepper")
            self.assertNotIn("_", issued["key_id"])
            self.assertEqual(
                self.store.authenticate(f"Bearer {issued['api_key']}", "test-pepper"), tenant
            )

    def test_authentication_rejects_malformed_and_expired_keys(self):
        tenant = self.store.create_tenant("Expiring Consumer")["id"]
        expired = self.store.issue_api_key(tenant, "test-pepper", expires_at="2000-01-01T00:00:00+00:00")
        rejected = [None, "", "Basic value", "Bearer", "Bearer cf_live_bad", "Bearer cf_test_a_b",
                    f"Bearer {expired['api_key']}"]
        for authorization in rejected:
            with self.subTest(authorization=authorization), self.assertRaises(AuthenticationError):
                self.store.authenticate(authorization, "test-pepper")

    def test_duplicate_tenant_names_are_rejected(self):
        self.store.create_tenant("Duplicate")
        with self.assertRaises(ValidationError):
            self.store.create_tenant("Duplicate")

    def test_v1_database_migrates_existing_records_to_local_tenant(self):
        legacy_path = self.root / "legacy.db"
        record_id = "ef187c3d-5138-4c87-a92c-d9a14582541a"
        with closing(sqlite3.connect(legacy_path)) as db:
            db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                       "title TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL, "
                       "status TEXT NOT NULL, evidence TEXT NOT NULL, metadata TEXT NOT NULL, "
                       "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("INSERT INTO records VALUES (?, 'decision', 'Legacy', 'Body', 'user', "
                       "'active', '[]', '{}', '2026-01-01', '2026-01-01')", (record_id,))
            db.commit()
        migrated = ContextStore(legacy_path)
        self.assertEqual(migrated.get(record_id)["tenant_id"], "local")
        self.assertEqual(migrated.health()["schema_version"], 2)

    def test_lock_contention_fails_after_bounded_retries(self):
        with closing(sqlite3.connect(self.store.path, timeout=0.1)) as locker:
            locker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(StoreUnavailableError):
                self.store.add("decision", "Locked", "Must not hang", "user")
            locker.rollback()


if __name__ == "__main__":
    unittest.main()
