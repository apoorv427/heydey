"""Connector sync — the S6b pull loop that turns a live MCP server into corpus.

The full pipeline on the proven S6a security floor, one function end to end:

  spawn (MCPHost) -> list_tools -> for every PULL-classed tool: call_tool (which
  ALREADY guards the result — Layer 1) -> slice the guarded context block ->
  guard the chunks again (PII/injection, belt + suspenders) -> tag them
  source_type=connector + connector_id -> delete_document + store_chunks
  (idempotent re-sync) -> graph.index_document (entities @ ingest, Contract B).

Two rails are load-bearing and NEVER bypassed:

- Layer 2 (approval): only ``approval_class == "none"`` tools are pulled. A
  write/spend tool is skipped here — it can only run through an approved tray
  row, and a background sync must never present one (no surface forgets it).
- Layer 1 (injection): a FLAGGED result is already stored + quarantined by the
  guard inside ``call_tool``; we count it and refuse to chunk it, so the attack
  text lands in ``mcp_results`` for audit and NEVER in the retrievable corpus.

Per-tool isolation (Contract B): one failing tool logs and the sync continues —
a single bad tool can't abort a connector's whole pull.

``sync`` registers the connector if it isn't already (idempotent, non-clobbering),
so the Live Map — driven by the ``connectors`` table joined to ``mcp_results`` —
reconciles after a bare ``sync(...)`` with no separate register step (the S6b
gate calls sync directly). stdlib + existing modules only (L6).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

from . import connectors, graph, slicer
from .ingest_guard import guard_chunks
from .mcp_host import MCPHost
from .vector_store import delete_document, store_chunks

log = logging.getLogger(__name__)

# Local MCP servers we know how to spawn by name (register/sync by connector id).
KNOWN_SERVERS: dict[str, list[str]] = {
    "demo-shopify": [sys.executable, "-m", "heydey.demo_connector"],
    "demo-sheets": [sys.executable, "-m", "heydey.demo_sheets"],
    "demo-agency": [sys.executable, "-m", "heydey.demo_agency"],
}

# The bundled demo/onboarding MCP servers carry SYNTHETIC data. Their rows must
# never reach a real, document-backed workspace — that is exactly the isolation
# invariant the S6b/S6c/S7 gates assert ("<real ws> connector chunks == 0").
_SYNTHETIC_CONNECTORS = frozenset(KNOWN_SERVERS)


def _protected_workspaces() -> frozenset[str]:
    """Workspaces a synthetic connector may NOT sync into (the production
    corpora). Defaults to the server's default workspace; override with the
    comma-separated ``HEYDEY_PROTECTED_WORKSPACES`` env var."""
    raw = os.environ.get("HEYDEY_PROTECTED_WORKSPACES", "blueleaf")
    return frozenset(w.strip() for w in raw.split(",") if w.strip())


def _assert_sync_allowed(workspace_id: str, connector_id: str) -> None:
    """Fail closed before a demo/synthetic connector can pollute a real corpus.

    This is the automated guard for the 2026-07-25 leak class: ``POST
    /connectors/sync`` defaults ``workspace`` to the production workspace, so a
    background or demo-prep sync with no explicit workspace would land synthetic
    rows in it. The gates sync demo connectors into ephemeral workspaces only, so
    this never blocks them."""
    if connector_id in _SYNTHETIC_CONNECTORS and workspace_id in _protected_workspaces():
        raise connectors.ConnectorError(
            f"refusing to sync synthetic connector {connector_id!r} into protected "
            f"workspace {workspace_id!r} — demo data must never enter a real corpus "
            f"(override with HEYDEY_PROTECTED_WORKSPACES)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync(conn: sqlite3.Connection, workspace_id: str, connector_id: str,
         command: list[str]) -> dict:
    """Pull every pull-classed tool of one connector into the workspace corpus.

    Returns a sync report: how many tools were pulled, how many chunks landed,
    how many results were flagged (quarantined, never chunked), how many graph
    entities were extracted, and when. Idempotent: re-syncing rewrites the same
    per-tool document (delete_document + deterministic chunk ids) so the chunk
    count does not grow, while ``mcp_results`` keeps every pull as an audit row.
    """
    _assert_sync_allowed(workspace_id, connector_id)  # fail closed: no demo data in a real corpus
    synced_at = _now()
    # Register-if-needed, non-clobbering: an explicit prior register (with real
    # scopes) is preserved; a bare sync still makes the connector appear on the
    # Live Map, which the S6b gate exercises without a separate register call.
    if connectors.get(conn, workspace_id, connector_id) is None:
        connectors.register(conn, workspace_id, connector_id)

    tools_pulled = chunks = flagged = entities = 0
    with MCPHost(workspace_id=workspace_id, connector_id=connector_id,
                 command=command) as host:
        for tool in host.list_tools():
            if tool["approval_class"] != "none":
                continue  # Layer 2: write/spend tools never auto-run on a sync
            name = tool["name"]
            try:
                result = host.call_tool(conn, name)  # already guarded (Layer 1)
                tools_pulled += 1
                if result["injection_risk"]:
                    flagged += 1  # stored + quarantined by the guard; do NOT chunk
                    continue
                doc_id = f"connector:{connector_id}:{name}"
                pieces = slicer.slice_document(
                    result["context_block"], doc_id=doc_id,
                    title=f"{connector_id} · {name}", source_file=doc_id,
                    created_at=synced_at)
                pieces = guard_chunks(pieces)  # PII/injection tag, belt + suspenders
                for piece in pieces:
                    piece["source_type"] = "connector"
                    piece["connector_id"] = connector_id
                delete_document(conn, doc_id)  # idempotent re-sync
                chunks += store_chunks(conn, pieces, doc_id)
                entities += graph.index_document(
                    conn, doc_id, result["context_block"], workspace_id)
            except Exception as exc:  # per-tool isolation (Contract B)
                log.warning("connector %s tool %s failed to sync: %s",
                            connector_id, name, exc)
                continue

    return {"connector_id": connector_id, "tools_pulled": tools_pulled,
            "chunks": chunks, "flagged": flagged, "entities": entities,
            "synced_at": synced_at}


def live_map(conn: sqlite3.Connection, workspace_id: str) -> list[dict]:
    """One Live Map row per registered connector: the ``connectors`` table joined
    to its ``mcp_results`` (result count · flagged count · last sync) and its
    corpus footprint (chunk count). Read-only; the workspace's db file IS the
    isolation boundary, so a fresh workspace returns []."""
    rows: list[dict] = []
    for c in connectors.list_connectors(conn, workspace_id):
        cid = c["connector_id"]
        # mcp_host stores results under "{workspace_id}.{connector_id}"; the db
        # file already scopes us to one workspace, so match that exact key.
        results, flagged, last_sync = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(injection_risk), 0), MAX(received_at)"
            " FROM mcp_results WHERE connector_id = ?",
            (f"{workspace_id}.{cid}",),
        ).fetchone()
        chunks = conn.execute(
            "SELECT COUNT(*) FROM points WHERE json_extract(payload, '$.connector_id') = ?",
            (cid,),
        ).fetchone()[0]
        rows.append({
            "connector_id": cid,
            "keychain_ref": c["keychain_ref"],
            "scopes": c["scopes"],
            "results": results,
            "flagged": flagged,
            "last_sync": last_sync,
            "chunks": chunks,
        })
    return rows
