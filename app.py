from __future__ import annotations

import json
import logging
import os
import uuid
from functools import wraps
from pathlib import Path
from typing import Optional

import gradio as gr

try:
    import spaces
except ImportError:  # The package is injected only by Hugging Face ZeroGPU.
    spaces = None

from contextforge import (AuthenticationError, ContextStore, NotFoundError,
                          StoreError, ValidationError)


DB_PATH = os.getenv("CONTEXTFORGE_DB", str(Path("data") / "contextforge.db"))
BACKUP_DIR = os.getenv("CONTEXTFORGE_BACKUP_DIR", str(Path("data") / "backups"))
BACKUP_KEEP = int(os.getenv("CONTEXTFORGE_BACKUP_KEEP", "5"))
STORAGE_MODE = os.getenv("CONTEXTFORGE_STORAGE_MODE", "self_hosted").strip().lower()
KEY_PEPPER = os.getenv("CONTEXTFORGE_KEY_PEPPER", "")
if STORAGE_MODE not in {"self_hosted", "hosted"}:
    raise RuntimeError("CONTEXTFORGE_STORAGE_MODE must be self_hosted or hosted")
if STORAGE_MODE == "hosted" and not KEY_PEPPER:
    raise RuntimeError("CONTEXTFORGE_KEY_PEPPER is required in hosted mode")
store = ContextStore(
    DB_PATH,
    busy_timeout_ms=int(os.getenv("CONTEXTFORGE_BUSY_TIMEOUT_MS", "5000")),
    write_retries=int(os.getenv("CONTEXTFORGE_WRITE_RETRIES", "5")),
)
logging.basicConfig(level=os.getenv("CONTEXTFORGE_LOG_LEVEL", "INFO"))
logger = logging.getLogger("contextforge")


def zero_gpu_startup_probe() -> str:
    """Satisfy ZeroGPU startup detection; ContextForge itself is CPU-only."""
    return "ContextForge is ready. No GPU workload is required."


if spaces is not None:
    # Personal-account Gradio Spaces may be assigned ZeroGPU even for CPU-only
    # apps. Register one private, short probe so the ZeroGPU supervisor accepts
    # startup. It is never exposed as an API or MCP tool.
    zero_gpu_startup_probe = spaces.GPU(duration=1)(zero_gpu_startup_probe)


def _json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _tenant(authorization: Optional[gr.Header] = None) -> str:
    """Resolve a trusted tenant without exposing tenant_id as an MCP argument."""
    if STORAGE_MODE == "self_hosted":
        return "local"
    return store.authenticate(authorization, KEY_PEPPER)


def resilient_tool(function):
    """Convert expected failures into stable JSON without leaking internals."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValidationError as exc:
            return _json({"ok": False, "error": {"code": "validation_error", "message": str(exc)}})
        except AuthenticationError as exc:
            return _json({"ok": False, "error": {"code": "unauthorized", "message": str(exc)}})
        except NotFoundError as exc:
            return _json({"ok": False, "error": {"code": "not_found", "message": str(exc)}})
        except StoreError as exc:
            incident_id = str(uuid.uuid4())
            logger.exception("Store failure; incident=%s", incident_id)
            return _json({"ok": False, "error": {"code": "store_unavailable",
                                                   "message": "Project memory is temporarily unavailable.",
                                                   "incident_id": incident_id}})
        except Exception:
            incident_id = str(uuid.uuid4())
            logger.exception("Unexpected tool failure; incident=%s", incident_id)
            return _json({"ok": False, "error": {"code": "internal_error",
                                                   "message": "The operation could not be completed.",
                                                   "incident_id": incident_id}})
    return wrapped


@resilient_tool
def search_project(query: str, limit: int = 20,
                   authorization: Optional[gr.Header] = None) -> str:
    """Search shared project memory for relevant decisions, work packets, and implementation notes.

    Args:
        query: Words or phrases to find in titles, content, or evidence citations.
        limit: Maximum number of matching records, from 1 to 100.
    """
    return _json(store.search(query, limit, tenant_id=_tenant(authorization)))


@resilient_tool
def record_decision(title: str, rationale: str, source: str, evidence: str = "",
                    authorization: Optional[gr.Header] = None) -> str:
    """Record an architectural or product decision for all connected agents.

    Args:
        title: Concise decision title.
        rationale: Decision, context, alternatives, and reasoning.
        source: Author, such as user, Claude, or Codex.
        evidence: Optional comma-separated file paths, URLs, or references.
    """
    citations = [item.strip() for item in evidence.split(",") if item.strip()]
    return _json(store.add("decision", title, rationale, source, evidence=citations,
                           tenant_id=_tenant(authorization)))


@resilient_tool
def list_decisions(limit: int = 50, authorization: Optional[gr.Header] = None) -> str:
    """List the newest project decisions.

    Args:
        limit: Maximum number of decisions, from 1 to 200.
    """
    return _json(store.list("decision", limit, tenant_id=_tenant(authorization)))


@resilient_tool
def create_work_packet(
    objective: str,
    requirements: str,
    acceptance_tests: str,
    source: str,
    relevant_files: str = "",
    authorization: Optional[gr.Header] = None,
) -> str:
    """Create a structured implementation handoff for another coding or planning agent.

    Args:
        objective: The outcome the receiving agent should achieve.
        requirements: Newline-separated requirements and constraints.
        acceptance_tests: Newline-separated checks that define completion.
        source: Author, such as user, Claude, or Codex.
        relevant_files: Optional comma-separated repository paths.
    """
    metadata = {
        "requirements": [line.strip() for line in requirements.splitlines() if line.strip()],
        "acceptance_tests": [line.strip() for line in acceptance_tests.splitlines() if line.strip()],
        "relevant_files": [item.strip() for item in relevant_files.split(",") if item.strip()],
    }
    return _json(store.add("work_packet", objective, requirements, source, status="ready",
                           metadata=metadata, tenant_id=_tenant(authorization)))


@resilient_tool
def get_work_packet(record_id: str, authorization: Optional[gr.Header] = None) -> str:
    """Retrieve one work packet by its exact identifier.

    Args:
        record_id: UUID returned by create_work_packet.
    """
    record = store.get(record_id, tenant_id=_tenant(authorization))
    if record["kind"] != "work_packet":
        raise ValidationError("record is not a work packet")
    return _json(record)


@resilient_tool
def record_implementation(
    title: str,
    summary: str,
    changed_files: str,
    tests: str,
    source: str,
    authorization: Optional[gr.Header] = None,
) -> str:
    """Record completed implementation work so other agents can verify and continue it.

    Args:
        title: Concise name of the implementation.
        summary: What changed, tradeoffs, and any remaining work.
        changed_files: Comma-separated changed repository paths.
        tests: Tests run and their outcomes.
        source: Author, such as Codex or Claude.
    """
    files = [item.strip() for item in changed_files.split(",") if item.strip()]
    return _json(store.add("implementation", title, summary, source, evidence=files,
                           metadata={"tests": tests}, tenant_id=_tenant(authorization)))


@resilient_tool
def project_summary(authorization: Optional[gr.Header] = None) -> str:
    """Return counts and recency information for the shared project memory."""
    return _json(store.summary(tenant_id=_tenant(authorization)))


@resilient_tool
def update_record_status(record_id: str, status: str,
                         authorization: Optional[gr.Header] = None) -> str:
    """Update a record lifecycle status after validating its identifier and transition value.

    Args:
        record_id: UUID of a decision, work packet, or implementation record.
        status: One of active, ready, in_progress, complete, superseded, or blocked.
    """
    return _json(store.update_status(record_id, status, tenant_id=_tenant(authorization)))


@resilient_tool
def health_check(authorization: Optional[gr.Header] = None) -> str:
    """Check database reachability, integrity, schema version, size, and query latency."""
    tenant_id = _tenant(authorization)
    return _json({**store.health(), "storage_mode": STORAGE_MODE,
                  "tenant_authenticated": tenant_id != "local"})


@resilient_tool
def backup_database(authorization: Optional[gr.Header] = None) -> str:
    """Create an integrity-checked SQLite backup and prune backups beyond retention."""
    _tenant(authorization)
    return _json(store.backup(BACKUP_DIR, keep=BACKUP_KEEP))


def refresh_dashboard(authorization: Optional[gr.Header] = None):
    if STORAGE_MODE == "hosted" and not authorization:
        return ("### Hosted tenant mode\n\nConnect through MCP with your ContextForge API key. "
                "The browser dashboard does not accept keys in forms.", [])
    tenant_id = _tenant(authorization)
    rows = store.list(limit=100, tenant_id=tenant_id)
    table = [
        [row["id"], row["kind"], row["title"], row["source"], row["status"], row["updated_at"]]
        for row in rows
    ]
    summary = store.summary(tenant_id=tenant_id)
    health = store.health()
    text = (f"### {summary['total']} records\n\nLast updated: {summary['last_updated'] or 'Never'}"
            f"\n\nService health: **{health['status']}**")
    return text, table


with gr.Blocks(title="ContextForge") as demo:
    gr.Markdown(
        "# ContextForge\nShared, evidence-aware project memory for Claude and Codex. "
        "Every form below is also a native MCP tool."
    )
    summary_view = gr.Markdown()
    refresh = gr.Button("Refresh dashboard")
    records = gr.Dataframe(
        headers=["ID", "Kind", "Title", "Source", "Status", "Updated"],
        datatype=["str"] * 6,
        interactive=False,
    )
    refresh.click(
        refresh_dashboard,
        outputs=[summary_view, records],
        queue=False,
        api_name=None,
        api_visibility="private",
    )
    demo.load(
        refresh_dashboard,
        outputs=[summary_view, records],
        queue=False,
        api_visibility="private",
    )

    with gr.Tab("Search"):
        q = gr.Textbox(label="Search project memory")
        search_limit = gr.Slider(1, 100, value=20, step=1, label="Limit")
        search_output = gr.Code(language="json", label="Results")
        gr.Button("Search").click(
            search_project, [q, search_limit], search_output, queue=False, api_name="search_project"
        )

    with gr.Tab("Decision"):
        decision_title = gr.Textbox(label="Title")
        rationale = gr.Textbox(label="Rationale", lines=6)
        decision_source = gr.Textbox(value="user", label="Source")
        decision_evidence = gr.Textbox(value="", label="Evidence (comma-separated)")
        decision_output = gr.Code(language="json", label="Saved decision")
        gr.Button("Record decision").click(
            record_decision,
            [decision_title, rationale, decision_source, decision_evidence],
            decision_output,
            queue=False,
            api_name="record_decision",
        )

    with gr.Tab("Work packet"):
        objective = gr.Textbox(label="Objective")
        requirements = gr.Textbox(label="Requirements", lines=5)
        acceptance = gr.Textbox(label="Acceptance tests", lines=5)
        packet_source = gr.Textbox(value="user", label="Source")
        relevant_files = gr.Textbox(value="", label="Relevant files (comma-separated)")
        packet_output = gr.Code(language="json", label="Saved work packet")
        gr.Button("Create work packet").click(
            create_work_packet,
            [objective, requirements, acceptance, packet_source, relevant_files],
            packet_output,
            queue=False,
            api_name="create_work_packet",
        )

    with gr.Tab("Implementation"):
        implementation_title = gr.Textbox(label="Title")
        implementation_summary = gr.Textbox(label="Summary", lines=5)
        changed_files = gr.Textbox(label="Changed files (comma-separated)")
        tests = gr.Textbox(label="Tests and results", lines=3)
        implementation_source = gr.Textbox(value="Codex", label="Source")
        implementation_output = gr.Code(language="json", label="Saved implementation")
        gr.Button("Record implementation").click(
            record_implementation,
            [implementation_title, implementation_summary, changed_files, tests, implementation_source],
            implementation_output,
            queue=False,
            api_name="record_implementation",
        )

    # Hidden event handlers become API endpoints and therefore native MCP tools.
    with gr.Column(visible=False):
        hidden_limit = gr.Number(value=50, precision=0)
        hidden_record_id = gr.Textbox()
        hidden_output = gr.Textbox()
        gr.Button("List decisions").click(
            list_decisions, hidden_limit, hidden_output, queue=False, api_name="list_decisions"
        )
        gr.Button("Get work packet").click(
            get_work_packet, hidden_record_id, hidden_output, queue=False, api_name="get_work_packet"
        )
        gr.Button("Project summary").click(
            project_summary, outputs=hidden_output, queue=False, api_name="project_summary"
        )
        hidden_status = gr.Textbox()
        gr.Button("Update record status").click(
            update_record_status, [hidden_record_id, hidden_status], hidden_output,
            queue=False, api_name="update_record_status"
        )
        gr.Button("Health check").click(
            health_check, outputs=hidden_output, queue=False, api_name="health_check"
        )
        gr.Button("Backup database").click(
            backup_database, outputs=hidden_output, queue=False, api_name="backup_database"
        )
        gr.Button("ZeroGPU startup probe").click(
            zero_gpu_startup_probe,
            outputs=hidden_output,
            queue=False,
            api_name=None,
            api_visibility="private",
        )


if __name__ == "__main__":
    # Spaces enables SSR by default, but this MCP-first app does not need the
    # Node.js proxy. Client-side rendering keeps a single Python server alive
    # on port 7860 and avoids SSR lifecycle failures on CPU Spaces.
    demo.launch(mcp_server=True, ssr_mode=False, pwa=False)
