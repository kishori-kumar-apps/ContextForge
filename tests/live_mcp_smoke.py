"""Live hosted-mode MCP smoke test. Requires a running ContextForge server."""

from __future__ import annotations

import json
import os

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def result_json(result):
    return json.loads(result.content[0].text)


async def connect(headers):
    url = os.environ["CONTEXTFORGE_SMOKE_URL"]
    return streamablehttp_client(url, headers=headers, timeout=10, sse_read_timeout=10)


async def main():
    key_a, key_b = os.environ["CONTEXTFORGE_SMOKE_KEY_A"], os.environ["CONTEXTFORGE_SMOKE_KEY_B"]
    async with await connect({"Authorization": f"Bearer {key_a}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"record_decision", "search_project", "project_summary"} <= names, names
            created = result_json(await session.call_tool("record_decision", {
                "title": "Live MCP isolation", "rationale": "Network path works",
                "source": "integration-test", "evidence": "tests/live_mcp_smoke.py",
            }))
            assert created["title"] == "Live MCP isolation", created
            summary_a = result_json(await session.call_tool("project_summary", {}))
            assert summary_a["total"] == 1, summary_a

    async with await connect({"Authorization": f"Bearer {key_b}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            summary_b = result_json(await session.call_tool("project_summary", {}))
            assert summary_b["total"] == 0, summary_b
            search_b = result_json(await session.call_tool("search_project", {
                "query": "Live MCP isolation", "limit": 20,
            }))
            assert search_b == [], search_b

    async with await connect({"Authorization": "Bearer cf_live_invalid_value"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            rejected = result_json(await session.call_tool("project_summary", {}))
            assert rejected["error"]["code"] == "unauthorized", rejected

    print(json.dumps({"ok": True, "tools": len(names), "tenant_a_total": 1,
                      "tenant_b_total": 0, "invalid_key_rejected": True}))


if __name__ == "__main__":
    anyio.run(main)
