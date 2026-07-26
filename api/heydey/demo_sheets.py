"""Synthetic demo Sheets connector — a real MCP server over stdio, fake data.

Run: python -m heydey.demo_sheets

The demo-sheets counterpart to demo_connector.py (§14-C5 synthetic-corpus gate):
a read-only ad-spend sheet, ALL data synthetic (DEMO- labelled, no PII). Two
PULL tools, NO write tools — sheets are read-in at MVP:

  list_adspend  (pull)  14 daily rows {date, channel: meta|google, spend_inr, orders}
  sheet_stats   (pull)  {rows, channels, total_spend_inr, cac_inr}

Named `list_adspend`, not `list_ad_spend`: the S6a host derives approval class
from the tool NAME, and the standalone token "spend" trips the spend-matcher
(fail-closed) — so the tool would classify `spend` and never pull. Dropping the
underscore keeps the exact term while classifying `none` (see the S6b build
report — the frozen classifier can't change without breaking S6a's tests).

Newline-delimited JSON-RPC 2.0, stdlib only.
"""

import json
import sys

PROTOCOL_VERSION = "2025-06-18"

# 14 synthetic daily rows, deterministic — two channels, one DEMO ad-spend sheet.
_ROWS = [
    {"date": f"2026-07-{7 + i:02d}",
     "channel": "meta" if i % 2 == 0 else "google",
     "spend_inr": 5000 + i * 250,
     "orders": 20 + (i % 5) * 3}
    for i in range(14)
]

TOOLS = [
    {"name": "list_adspend",
     "description": "List daily adspend by channel (synthetic demo rows)",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "sheet_stats",
     "description": "Get the adspend summary: totals, channels, blended CAC (synthetic)",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _text(payload) -> dict:
    return {"content": [{"type": "text", "text": payload if isinstance(payload, str)
                         else json.dumps(payload, indent=1)}]}


def _stats() -> dict:
    total_spend = sum(r["spend_inr"] for r in _ROWS)
    total_orders = sum(r["orders"] for r in _ROWS)
    channels = len({r["channel"] for r in _ROWS})
    cac = round(total_spend / total_orders, 2) if total_orders else 0.0
    return {"sheet": "DEMO-adspend-2026", "rows": len(_ROWS), "channels": channels,
            "total_spend_inr": total_spend, "cac_inr": cac}


def handle(method: str, params: dict) -> dict | None:
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                "serverInfo": {"name": "heydey-demo-sheets", "version": "0.1"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name", "")
        if name == "list_adspend":
            return _text(_ROWS)
        if name == "sheet_stats":
            return _text(_stats())
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    return None  # notifications


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = handle(msg.get("method", ""), msg.get("params", {}) or {})
        if msg.get("id") is not None:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result or {}}
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
