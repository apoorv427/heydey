"""Heydey as an MCP SERVER — the proof engine exposed to any MCP client (Claude
Code, Claude Desktop, Cursor, ...). Until now Heydey was only an MCP *client*
(:mod:`heydey.mcp_host`, calling out to connectors); this module is the other
direction: a foreign agent calls INTO Heydey.

Hand-built stdio JSON-RPC 2.0, newline-delimited — same framing as
:mod:`heydey.mcp_host` and :mod:`heydey.demo_connector` (L6: no MCP SDK).
``PROTOCOL_VERSION`` is imported from :mod:`heydey.mcp_host`, the single
source of truth for the version string this codebase speaks.

READ-ONLY TOOL SURFACE (by design, not by omission — Contract C's whole point
is that a human approves outbound/spend actions; a remote LLM approving its
own action inverts that). Exactly three tools, all pull-shaped:

    heydey_ask    — cited, cross-model-validated answer + receipts, or an
                    honest refusal when nothing grounds an answer.
    heydey_find   — doc-level ranked search (source/chunk breadcrumbs).
    heydey_today  — the latest Morning Brief (cited, deterministic, no LLM).

No approvals/decide, no connector sync, no external or exec-class action is
ever registered here. ``TOOLS`` is self-checked against
:func:`heydey.mcp_host.classify_tool` at import time (below) so a future edit
that adds a write/send-shaped tool name fails at IMPORT, not in production.

Precisely what "read-only" means here (an adversarial verifier caught the
earlier wording claiming "no writes", which was false): read-only refers to
the OUTSIDE world and to the approval boundary — nothing here can act on a
connector, approve a tray row, spend, send, or touch a file. ``heydey_ask``
does write LOCALLY, inside the caller's own workspace db, because answering
is what produces a receipt: it appends to ``receipts``, ``costs``,
``sessions`` and the graph's co-activity edges, exactly as the same question
asked in the UI would. That ledger IS the product; a tool that answered
without leaving a receipt would be the actual defect. ``heydey_find`` and
``heydey_today`` are pure reads.

── OUTBOUND GUARD (Contract C Layer 1, extended to the read surface) ─────────
S6a guarded INBOUND content only (ingest_guard on every ingest lane, plus
mcp_host.call_tool guarding CONNECTOR results). Nothing guarded what THIS
process hands back OUT to a foreign agent — that gap is closed here.
Every string this server returns passes through :func:`_outbound_guard`
before it leaves the process:

  1. PII re-scrub (``ingest_guard.scrub_pii``) — belt-and-suspenders on top
     of the ingest-time scrub.
  2. secret redaction — masks any value this process has served via
     :mod:`heydey.secrets_store` (Keychain/env-file lookups this process
     itself performed), mirroring ``SecretRedactionFilter`` but applied to
     outbound TEXT rather than log records.
  3. an injection RE-SCAN (``ingest_guard.scan_injection``) on every outbound
     string, independent of any upstream ``injection_risk`` tag. This is NOT
     redundant: ``heydey_ask``/``heydey_find`` retrieve through
     ``ask.retrieve()``, which already excludes ``injection_risk`` chunks —
     but ``heydey_today`` reads ``morning_brief.latest()``, whose
     ``_section_gates``/``_section_locks`` query ``points`` via FTS DIRECTLY
     and carry NO injection exclusion of their own. Without this re-scan, a
     poisoned doc that also happens to contain a watched keyword ("GATE",
     "PENDING", "CEO owes") would ship straight to a foreign agent through
     the brief. A hit renders ``[flagged-source withheld ...]``, never the
     raw text — the same posture ``ingest_guard.guard_mcp_result`` uses for
     ``mcp_results``.

Refusals are first-class: when a tool cannot ground an answer (no evidence,
no brief run yet, no documents matched), the response carries
``refused: true`` + a human-readable ``reason`` — never an empty success.

── AUTH / CLIENT CONFIG ───────────────────────────────────────────────────────
Transport is local stdio: the client SPAWNS this process directly (the same
way any stdio MCP server works), so process-spawn IS the trust boundary —
there is no bearer token gating a ``tools/call`` today, the stdio analogue of
``auth.py``'s own posture ("reads pass freely, the 127.0.0.1 bind is their
boundary"; here, the subprocess handle is that boundary. Heydey's HTTP
supervisor DOES mint a fresh bearer token every launch
(``auth.generate_token()``, written to ``~/.heydey/runtime/supervisor.json``,
0600) for its own MUTATING HTTP routes — if this server is ever fronted by a
network transport instead of stdio, reuse that token. Note honestly: it is
regenerated on every supervisor restart, so it can never be hardcoded into a
shared client config; a pasted config with a fixed token value will go stale
the next time the supervisor restarts.

Paste this into an MCP client's server config (see ``CLIENT_CONFIG_EXAMPLE``):

    {
      "mcpServers": {
        "heydey": {
          "command": "python3",
          "args": ["-m", "heydey.mcp_server"],
          "env": {"HEYDEY_HOME": "/Users/you/.heydey"}
        }
      }
    }

Workspace is caller-chosen PER CALL (the ``workspace`` argument on every
tool), validated against the real workspace list via
``heydey.workspaces.connect`` — a bogus workspace id fails closed with a
clear ``refused``/error, never a crash.
"""

from __future__ import annotations

import json
import sys

from . import ask as ask_mod
from . import ingest_guard, models_state, morning_brief, pipeline, secrets_store, workspaces
from .mcp_host import PROTOCOL_VERSION, classify_tool

SERVER_NAME = "heydey"
SERVER_VERSION = "0.1"

CLIENT_CONFIG_EXAMPLE = """{
  "mcpServers": {
    "heydey": {
      "command": "python3",
      "args": ["-m", "heydey.mcp_server"],
      "env": {"HEYDEY_HOME": "/Users/you/.heydey"}
    }
  }
}"""

TOOLS = [
    {
        "name": "heydey_ask",
        "description": (
            "Query this workspace's knowledge base with a question. Returns a "
            "cited, cross-model-validated answer with receipts (source, chunk, "
            "retrieval score, validator badge, cost) — or an honest refusal "
            "when nothing in the corpus grounds an answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace id"},
                "question": {"type": "string"},
                "k": {"type": "integer", "default": 6, "description": "chunks to retrieve"},
            },
            "required": ["workspace", "question"],
        },
    },
    {
        "name": "heydey_find",
        "description": (
            "Search this workspace's documents (doc-level, ranked). Returns "
            "matching documents with source/chunk breadcrumbs and snippets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5, "description": "documents to return"},
            },
            "required": ["workspace", "query"],
        },
    },
    {
        "name": "heydey_today",
        "description": (
            "Read this workspace's Morning Brief — the latest cited overnight "
            "summary (changed docs, open gates, locks, health, costs, "
            "sessions, approvals). Deterministic; no LLM ran to build it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"workspace": {"type": "string"}},
            "required": ["workspace"],
        },
    },
]

# Structural guard: every tool this server EVER declares must classify as a
# pull (approval_class == "none"). A future edit that adds a write/send-shaped
# tool name fails HERE, at import — this surface is read-only by construction,
# not by code-review vigilance.
for _t in TOOLS:
    _cls = classify_tool(_t["name"], _t["description"])
    assert _cls == "none", (
        f"{_t['name']} classifies as {_cls!r} — heydey.mcp_server is a READ-ONLY "
        "surface (Contract C); a mutating tool must never be registered here")
del _t, _cls


class ToolError(Exception):
    """A tool ran but failed for a business reason (bad workspace, ...) — becomes
    a normal JSON-RPC result with ``isError: true``, never a protocol error."""


class BadArguments(Exception):
    """Malformed tool arguments — a caller bug, becomes a JSON-RPC -32602 error."""


# ── outbound guard (see module docstring) ─────────────────────────────────────

def _redact_secrets(text: str) -> str:
    """Mask any value this process has served via secrets_store — mirrors
    SecretRedactionFilter, applied to outbound text instead of log records."""
    if not text:
        return text
    for value in secrets_store._served_values:
        if value and value in text:
            text = text.replace(value, secrets_store.REDACTED)
    return text


def _outbound_guard(text: str) -> str:
    """The ONE chokepoint every outbound string passes through: PII re-scrub,
    secret redaction, then an injection re-scan (independent of any upstream
    ``injection_risk`` tag — see module docstring for why that matters for
    ``heydey_today``). A hit is withheld, never shipped."""
    text = text or ""
    scrubbed, _findings = ingest_guard.scrub_pii(text)
    patterns = ingest_guard.scan_injection(scrubbed)
    if patterns:
        return f"[flagged-source withheld — injection pattern(s): {', '.join(patterns)}]"
    return _redact_secrets(scrubbed)


def _guard_citation(cite: dict) -> dict:
    cite = dict(cite)
    cite["snippet"] = _outbound_guard(cite.get("snippet", ""))
    return cite


def _guard_receipt(receipt: dict) -> dict:
    receipt = dict(receipt)
    receipt["claim_text"] = _outbound_guard(receipt.get("claim_text", ""))
    return receipt


def _guard_document(doc: dict) -> dict:
    doc = dict(doc)
    doc["chunks"] = [
        {**c, "snippet": _outbound_guard(c.get("snippet", ""))} for c in doc.get("chunks", [])
    ]
    return doc


def _guard_brief_item(item: dict) -> dict:
    item = dict(item)
    item["line"] = _outbound_guard(item.get("line", ""))
    return item


# ── argument helpers ───────────────────────────────────────────────────────────

def _as_int(value, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadArguments(f"'{field}' must be an integer") from None


def _connect(workspace: str):
    """Validate + open the caller-chosen workspace. Fails closed with a clear
    ToolError — never a raw traceback — on a bogus or missing id."""
    workspace = (workspace or "").strip()
    if not workspace:
        raise ToolError("'workspace' is required")
    try:
        return workspaces.connect(workspace)
    except workspaces.WorkspaceError as exc:
        raise ToolError(f"workspace {workspace!r} unavailable: {exc}") from exc


# ── tool implementations ───────────────────────────────────────────────────────

def _tool_ask(arguments: dict, *, executor_fn=None, validator_fn=None) -> dict:
    question = str(arguments.get("question", "")).strip()
    if not question:
        raise BadArguments("heydey_ask requires a non-empty 'question'")
    k = _as_int(arguments.get("k", 6), "k")
    workspace = str(arguments.get("workspace", "")).strip()
    conn = _connect(workspace)
    try:
        profile = models_state.active_profile()  # local Ollama by default — no cloud required
        spec = pipeline.AgentSpec(id="mcp-ask", name="MCP heydey_ask", task_class="ask", k=k)
        result = pipeline.run_pipeline(
            conn, spec, question, profile=profile, workspace_id=workspace,
            executor_fn=executor_fn, validator_fn=validator_fn,
        )
    finally:
        conn.close()

    if result.answer_kind == "empty":
        return {
            "refused": True,
            "reason": (f"No matching chunks in workspace {workspace!r} for that "
                      "question. Try broadening it, or ingest more documents."),
            "answer": "", "answer_kind": "empty", "citations": [], "receipts": [],
            "proof": {"validator": "n/a — no evidence retrieved", "cost_usd": 0.0,
                      "executor_model": None, "validator_model": None},
        }

    return {
        "refused": False,
        "answer": _outbound_guard(result.answer),
        "answer_kind": result.answer_kind,
        "citations": [_guard_citation(c) for c in result.citations],
        "receipts": [_guard_receipt(r) for r in result.receipts],
        "proof": {
            "validator_badge": result.badge,
            "validator_status": result.validator_status,
            "validator_pass": result.validator_pass,
            "executor_model": result.executor_model,
            "validator_model": result.validator_model,
            "cost_usd": result.cost_usd,
            "ungrounded_count": result.ungrounded_count,
            "retry_used": result.retry_used,
        },
    }


def _tool_find(arguments: dict, *, executor_fn=None, validator_fn=None) -> dict:
    query = str(arguments.get("query", "")).strip()
    if not query:
        raise BadArguments("heydey_find requires a non-empty 'query'")
    k = _as_int(arguments.get("k", 5), "k")
    workspace = str(arguments.get("workspace", "")).strip()
    conn = _connect(workspace)
    try:
        docs = ask_mod.find_docs(conn, query, k=k)
    finally:
        conn.close()

    proof = {"validator": "not applicable — document search, no synthesis", "cost_usd": 0.0}
    guarded = [_guard_document(d) for d in docs]
    if not guarded:
        return {"refused": True,
                "reason": f"No documents matched {query!r} in workspace {workspace!r}.",
                "documents": [], "proof": proof}
    return {"refused": False, "documents": guarded, "proof": proof}


def _tool_today(arguments: dict, *, executor_fn=None, validator_fn=None) -> dict:
    workspace = str(arguments.get("workspace", "")).strip()
    conn = _connect(workspace)
    try:
        brief = morning_brief.latest(conn)
    finally:
        conn.close()

    proof = {"validator": "n/a — Contract B: no LLM on the brief path", "cost_usd": 0.0}
    if brief is None:
        return {
            "refused": True,
            "reason": (f"No Morning Brief has run yet for workspace {workspace!r}. Run "
                      f"`python -m heydey.morning_brief --workspace {workspace}`, or "
                      "wait for the scheduled 07:00 job."),
            "items": [], "proof": proof,
        }
    return {
        "refused": False,
        "created_at": brief["created_at"],
        "items": [_guard_brief_item(i) for i in brief["items"]],
        "proof": proof,
    }


_TOOL_HANDLERS = {
    "heydey_ask": _tool_ask,
    "heydey_find": _tool_find,
    "heydey_today": _tool_today,
}


# ── JSON-RPC dispatch ───────────────────────────────────────────────────────────

def _text_block(payload: dict) -> dict:
    return {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}


def dispatch(request: dict, *, executor_fn=None, validator_fn=None) -> dict | None:
    """Handle one parsed JSON-RPC message. Returns the reply dict, or None for a
    notification (no id). Never raises — every failure becomes a JSON-RPC error
    object or an ``isError`` tool result, never a traceback reaching the transport.
    """
    msg_id = request.get("id") if isinstance(request, dict) else None
    method = request.get("method") if isinstance(request, dict) else None
    params = request.get("params") if isinstance(request, dict) else None
    params = params if isinstance(params, dict) else {}

    def _error(code: int, message: str) -> dict | None:
        if msg_id is None:
            return None  # notifications get no reply, even on error
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def _result(result) -> dict | None:
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    try:
        if method == "initialize":
            return _result({
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _result({"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                return _error(-32602, "tools/call requires a string 'name'")
            arguments = params.get("arguments", {})
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return _error(-32602, "'arguments' must be an object")
            handler = _TOOL_HANDLERS.get(name)
            if handler is None:
                return _error(-32601, f"unknown tool {name!r}")
            try:
                payload = handler(arguments, executor_fn=executor_fn, validator_fn=validator_fn)
                return _result({"content": [_text_block(payload)], "isError": False})
            except ToolError as exc:
                return _result({"content": [_text_block({"error": str(exc)})], "isError": True})
            except BadArguments as exc:
                return _error(-32602, str(exc))
        return _error(-32601, f"unknown method {method!r}")
    except Exception as exc:  # last resort — a traceback must never reach the transport
        return _error(-32603, f"internal error: {exc}")


def handle_line(line: str, *, executor_fn=None, validator_fn=None) -> dict | None:
    """Parse one newline-delimited JSON-RPC message and dispatch it. Returns the
    reply dict (or None for a blank line / notification). Never raises."""
    line = line.strip()
    if not line:
        return None
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        return {"jsonrpc": "2.0", "id": None,
               "error": {"code": -32700, "message": "parse error: invalid JSON"}}
    return dispatch(request, executor_fn=executor_fn, validator_fn=validator_fn)


def serve(*, executor_fn=None, validator_fn=None) -> int:
    """The stdio loop — mirrors demo_connector.main()'s framing exactly."""
    for raw in sys.stdin:
        reply = handle_line(raw, executor_fn=executor_fn, validator_fn=validator_fn)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    return serve()


if __name__ == "__main__":
    sys.exit(main())
