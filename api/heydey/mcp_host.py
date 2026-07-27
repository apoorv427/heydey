"""MCP host — hand-built stdio JSON-RPC client (L6: the PROTOCOL is a rail, no SDK).

One class, three moves: spawn a connector server process, handshake, call tools.
Security is structural, decided at MANIFEST-PARSE time (Executor Contract C):

- Layer 2 (outbound approval): every tool is classified when the manifest is
  read — pull-shaped names run free; write/send/spend-shaped names get
  ``approval_class: outbound|spend`` and :meth:`call_tool` REFUSES them unless
  an approved tray row (S4b approvals) is presented. The check lives in the
  host, not the UI — no surface can forget it.
- Layer 1 (injection guard): callers never touch a raw result. ``call_tool``
  returns ONLY the context block from ``ingest_guard.guard_mcp_result`` —
  flagged content is stored + excluded, the LLM sees a stored-reference.
- Layers 3/4 live in :mod:`heydey.connectors` (egress tag + workspace-scoped
  credentials); results land in the per-workspace db file by construction.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (the MCP stdio
framing). stdlib only: subprocess + json + threading for the read side.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
from dataclasses import dataclass, field

from . import approvals as approvals_mod
from . import risk
from .ingest_guard import guard_mcp_result

PROTOCOL_VERSION = "2025-06-18"

# Manifest-parse classification (Contract C Layer 2). Spend beats outbound;
# outbound beats none. Unknown verbs default to OUTBOUND — fail closed.
_SPEND = re.compile(r"\b(pay|charge|refund|purchase|spend|transfer)\b|_pay\b", re.I)
_WRITE = re.compile(
    r"\b(create|write|send|update|delete|post|publish|suppress|cancel|set|add|remove|upload)\b", re.I)
_PULL = re.compile(r"\b(list|get|read|search|fetch|pull|query|describe|count|show)\b", re.I)


def classify_tool(name: str, description: str = "") -> str:
    # snake_case is one \w+ word — split it, or "list_orders" never matches \blist\b
    # and every pull tool silently classifies outbound (found by the S6a tests)
    text = re.sub(r"[_\-./]", " ", f"{name} {description}")
    if _SPEND.search(text):
        return "spend"
    if _WRITE.search(text):
        return "outbound"
    if _PULL.search(text):
        return "none"
    return "outbound"  # unknown shape -> approval required (fail closed)


class MCPHostError(RuntimeError):
    pass


class ApprovalRequired(MCPHostError):
    """An outbound/spend tool was called without an approved tray row."""


@dataclass
class MCPHost:
    """One connector server process, workspace-bound."""

    workspace_id: str
    connector_id: str
    command: list[str]
    timeout: float = 20.0
    # name -> tier from the connector's manifest (W2). resolve_tier means a
    # declaration can only raise risk above verb-shape inference, never lower it.
    declared_tiers: dict[str, str] | None = None
    _proc: subprocess.Popen | None = None
    _tools: dict[str, dict] = field(default_factory=dict)
    _next_id: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── transport ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        init = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "heydey-host", "version": "0.1"},
        })
        if "protocolVersion" not in init:
            raise MCPHostError(f"bad initialize response from {self.connector_id}: {init}")
        self._notify("notifications/initialized", {})

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def __enter__(self) -> "MCPHost":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _request(self, method: str, params: dict) -> dict:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MCPHostError("host not started")
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method,
                                  "params": params})
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()

            timer = threading.Timer(self.timeout, self._proc.kill)
            timer.start()
            try:
                while True:
                    line = self._proc.stdout.readline()
                    if not line:
                        raise MCPHostError(
                            f"{self.connector_id} closed the pipe (timeout or crash)")
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # sub-process noise on stdout — skip, stay on protocol
                    if msg.get("id") != msg_id:
                        continue  # notification or stale id
                    if "error" in msg:
                        raise MCPHostError(f"{method} failed: {msg['error']}")
                    return msg.get("result", {})
            finally:
                timer.cancel()

    def _notify(self, method: str, params: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPHostError("host not started")
        self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method,
                                           "params": params}) + "\n")
        self._proc.stdin.flush()

    # ── manifest (Layer 2 classification happens HERE) ───────────────────────

    def list_tools(self) -> list[dict]:
        result = self._request("tools/list", {})
        manifest = []
        for tool in result.get("tools", []):
            name = tool.get("name", "")
            description = tool.get("description", "")
            entry = {
                "name": name,
                "description": description,
                "approval_class": classify_tool(name, description),
                "risk_tier": risk.resolve_tier(
                    (self.declared_tiers or {}).get(name), name, description),
            }
            self._tools[entry["name"]] = entry
            manifest.append(entry)
        return manifest

    # ── calls (Layers 1 + 2 enforced) ────────────────────────────────────────

    def call_tool(self, conn: sqlite3.Connection, name: str, arguments: dict | None = None,
                  *, approval_id: int | None = None) -> dict:
        """Call a tool. Returns {context_block, injection_risk, stored_id, approval_class}
        — never the raw result text (Layer 1).

        Outbound/spend tools REQUIRE an ``approved`` tray row id (Layer 2)."""
        if not self._tools:
            self.list_tools()
        tool = self._tools.get(name)
        if tool is None:
            raise MCPHostError(f"unknown tool {name!r} on {self.connector_id}")

        # Union of both gates, fail closed: the legacy approval class AND the
        # 4-tier taxonomy each independently force the tray (W2 — no relaxation).
        if tool["approval_class"] != "none" or risk.requires_approval(tool["risk_tier"]):
            if approval_id is None:
                raise ApprovalRequired(
                    f"{name} is {tool['approval_class']}-classed / risk "
                    f"{tool['risk_tier']} — a tray approval is required")
            row = conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None or row[0] != "approved":
                raise ApprovalRequired(
                    f"approval {approval_id} is {row[0] if row else 'missing'} — not approved")

        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        raw = "\n".join(
            block.get("text", "") for block in result.get("content", [])
            if block.get("type") == "text"
        )
        guarded = guard_mcp_result(conn, f"{self.workspace_id}.{self.connector_id}", name, raw)
        guarded["approval_class"] = tool["approval_class"]
        guarded["risk_tier"] = tool["risk_tier"]
        return guarded
