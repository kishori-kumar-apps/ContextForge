"""Offline tenant and API-key provisioning for ContextForge administrators."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from contextforge import ContextStore, StoreError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage ContextForge tenants and API keys")
    parser.add_argument("--db", default=os.getenv("CONTEXTFORGE_DB", str(Path("data") / "contextforge.db")))
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-tenant", help="Create an isolated consumer tenant")
    create.add_argument("--name", required=True)
    issue = commands.add_parser("issue-key", help="Issue a consumer key; the full key is shown once")
    issue.add_argument("--tenant-id", required=True)
    issue.add_argument("--expires-at", help="Optional UTC ISO-8601 expiration")
    revoke = commands.add_parser("revoke-key", help="Revoke a key by its non-secret key ID")
    revoke.add_argument("--key-id", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    store = ContextStore(args.db)
    try:
        if args.command == "create-tenant":
            result = store.create_tenant(args.name)
        elif args.command == "issue-key":
            result = store.issue_api_key(
                args.tenant_id, os.getenv("CONTEXTFORGE_KEY_PEPPER", ""),
                expires_at=args.expires_at,
            )
            result["warning"] = "Copy api_key now. ContextForge does not store the full key."
        else:
            store.revoke_api_key(args.key_id)
            result = {"revoked": True, "key_id": args.key_id}
    except StoreError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "result": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
