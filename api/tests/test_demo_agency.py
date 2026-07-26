"""S6c §F/B2 — the demo-agency MCP server: 4 pull-classed tools + Layer 1 quarantine.

The demo-agency counterpart to test_mcp_host.py: a real subprocess speaking real
newline JSON-RPC, all data synthetic (§14-C5 DEMO-labeled). Assertions prove:

  1. classify_tool exact-matches every (name, description) pair as "none"
     — the belt against a description edit silently reclassifying a tool.
  2. The manifest lands over the wire (list_tools) with those same classes.
  3. Every pull tool actually flows synthetic DEMO- data into a guarded block.
  4. poisoned_note triggers Contract-C Layer 1: stored in mcp_results for audit,
     tagged injection_risk=True, the LLM-facing block carries a stored-reference
     — never the raw payload — mirroring poisoned_feed on demo-shopify.
  5. KNOWN_SERVERS["demo-agency"] is the single-line hook the Foundry uses to
     spawn the connector by id (registered in connector_sync).
"""

import sys

import pytest

from heydey import connector_sync, workspaces
from heydey.demo_agency import TOOLS
from heydey.mcp_host import MCPHost, classify_tool

AGENCY_CMD = [sys.executable, "-m", "heydey.demo_agency"]

# Exact expected classification per tool — a manifest-shape test, not a shape guess.
EXPECTED_CLASSES = {
    "list_intake": "none",
    "get_brand_notes": "none",
    "list_deliverables": "none",
    "poisoned_note": "none",
}


@pytest.fixture()
def ws(heydey_home):
    workspaces.create_workspace("agencyws")
    conn = workspaces.connect("agencyws")
    yield "agencyws", conn
    conn.close()


@pytest.fixture()
def host(ws):
    ws_id, conn = ws
    with MCPHost(workspace_id=ws_id, connector_id="demo-agency", command=AGENCY_CMD) as h:
        yield h, conn


# ── 1. every (name, description) pair pull-classifies (offline unit) ─────────

def test_classify_every_tool_pull():
    """The manifest text alone must land as `none` for every tool — the guard
    against a future edit silently making a tool `outbound` (fail-closed) or
    `spend`. classify_tool is the S6a substrate; we pin its verdict here."""
    manifest = {t["name"]: classify_tool(t["name"], t["description"]) for t in TOOLS}
    assert manifest == EXPECTED_CLASSES


def test_poisoned_note_needs_pull_verb_in_description():
    """`poisoned_note` alone falls through to `outbound` (fail-closed). It runs
    free only because its description carries a pull verb ("Fetch …") — the
    exact belt demo_connector.py's `poisoned_feed` uses. If the description
    ever loses that verb, this test flips first."""
    assert classify_tool("poisoned_note") == "outbound"
    poisoned = next(t for t in TOOLS if t["name"] == "poisoned_note")
    assert poisoned["description"].lower().startswith("fetch")
    assert classify_tool("poisoned_note", poisoned["description"]) == "none"


# ── 2. manifest round-trip over a live subprocess (mirrors test_mcp_host.py) ─

def test_manifest_roundtrip_classifies(host):
    """The real JSON-RPC handshake + `tools/list` must produce the same class
    map — proves the transport preserves what classify_tool ruled offline."""
    h, _ = host
    manifest = {t["name"]: t["approval_class"] for t in h.list_tools()}
    assert manifest == EXPECTED_CLASSES


# ── 3. pull tools flow synthetic DEMO- data into a guarded context block ─────

def test_pull_tools_return_synthetic_demo_data(host):
    """Each pull tool must return DEMO-labeled synthetic data (§14-C5) — no PII,
    no real client names — and the guarded block must carry the payload text
    that later chunk-slicing turns into the analyst/watch agents' corpus."""
    h, conn = host

    intake = h.call_tool(conn, "list_intake")
    assert intake["approval_class"] == "none"
    assert intake["injection_risk"] is False
    assert "DEMO-Acme" in intake["context_block"]
    assert "budget_inr" in intake["context_block"]
    assert "deadline" in intake["context_block"]

    notes = h.call_tool(conn, "get_brand_notes")
    assert notes["injection_risk"] is False
    assert "voice" in notes["context_block"]
    assert "audience" in notes["context_block"]
    assert "reference" in notes["context_block"]
    assert "DEMO-" in notes["context_block"]

    deliverables = h.call_tool(conn, "list_deliverables")
    assert deliverables["injection_risk"] is False
    assert "deliverable" in deliverables["context_block"]
    assert "timeline" in deliverables["context_block"]
    assert "DEMO-" in deliverables["context_block"]


# ── 4. Layer 1 quarantine — poisoned_note stored + tagged + excluded ─────────

def test_poisoned_note_quarantined(host):
    """Contract C Layer 1, mirrored on demo-agency: the poisoned result is
    stored in mcp_results for audit, tagged injection_risk=True, and the
    LLM-facing context_block carries a stored-reference — not the raw payload.
    The raw text remains in the db for the audit trail."""
    h, conn = host
    result = h.call_tool(conn, "poisoned_note")
    assert result["approval_class"] == "none"
    assert result["injection_risk"] is True
    assert "ignore previous instructions" not in result["context_block"].lower()
    assert f"[connector_result_stored:{result['stored_id']}]" in result["context_block"]

    raw = conn.execute("SELECT raw_text FROM mcp_results WHERE id=?",
                       (result["stored_id"],)).fetchone()[0]
    assert "ignore previous instructions" in raw  # stored for audit, excluded from context


# ── 5. KNOWN_SERVERS registration — the Foundry spawn hook ───────────────────

def test_known_servers_entry_registered():
    """The single-line KNOWN_SERVERS["demo-agency"] hook must be present — this
    is what the Foundry (§A2 agency-brief playbook) uses to spawn the connector
    by id. The command shape mirrors demo-sheets/demo-shopify: run the module."""
    assert "demo-agency" in connector_sync.KNOWN_SERVERS
    cmd = connector_sync.KNOWN_SERVERS["demo-agency"]
    assert cmd == [sys.executable, "-m", "heydey.demo_agency"]
