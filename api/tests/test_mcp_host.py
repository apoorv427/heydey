"""S6a — the MCP security substrate (Executor Contract C, layers 1/2/3/4).

The demo connector is a REAL subprocess speaking real newline JSON-RPC — these
tests exercise the actual transport, not a stub. All data synthetic (§14-C5)."""

import sys

import pytest

from heydey import approvals, connectors, mcp_host, workspaces
from heydey.connectors import EgressError, egress_allows
from heydey.mcp_host import ApprovalRequired, MCPHost, classify_tool

DEMO_CMD = [sys.executable, "-m", "heydey.demo_connector"]


@pytest.fixture()
def ws(heydey_home):
    workspaces.create_workspace("mcpws")
    conn = workspaces.connect("mcpws")
    yield "mcpws", conn
    conn.close()


@pytest.fixture()
def host(ws):
    ws_id, conn = ws
    with MCPHost(workspace_id=ws_id, connector_id="demo", command=DEMO_CMD) as h:
        yield h, conn


# ── manifest classification (Layer 2 decided at parse time) ───────────────────

def test_classify_tool_shapes():
    assert classify_tool("list_orders") == "none"
    assert classify_tool("get_customer") == "none"
    assert classify_tool("create_discount") == "outbound"
    assert classify_tool("send_email") == "outbound"
    assert classify_tool("refund_order") == "spend"
    assert classify_tool("frobnicate_widget") == "outbound"  # unknown -> fail closed


def test_manifest_roundtrip_classifies(host):
    h, _ = host
    manifest = {t["name"]: t["approval_class"] for t in h.list_tools()}
    assert manifest == {"list_orders": "none", "order_stats": "none",
                        "create_discount": "outbound", "poisoned_feed": "none"}


# ── Layer 2: outbound approval ────────────────────────────────────────────────

def test_pull_tool_runs_free(host):
    h, conn = host
    result = h.call_tool(conn, "list_orders")
    assert result["approval_class"] == "none"
    assert "DEMO-1000" in result["context_block"]  # synthetic data flowed


def test_write_requires_tray(host):
    h, conn = host
    with pytest.raises(ApprovalRequired, match="outbound"):
        h.call_tool(conn, "create_discount", {"code": "X"})

    approval_id = approvals.create_approval(
        conn, action_class="outbound",
        payload={"kind": "connector_call", "title": "Create discount X",
                 "connector_id": "demo", "tool": "create_discount"})
    with pytest.raises(ApprovalRequired, match="pending"):
        h.call_tool(conn, "create_discount", {"code": "X"}, approval_id=approval_id)

    approvals.decide(conn, approval_id, "approved", workspace_id="mcpws")
    result = h.call_tool(conn, "create_discount", {"code": "X"}, approval_id=approval_id)
    assert "created" in result["context_block"]


# ── Layer 1: injection guard on every result ─────────────────────────────────

def test_injection_excluded_from_context(host):
    """Contract C test_injection_excluded: the poisoned result is stored + tagged,
    and the LLM-facing block carries a stored-reference, never the raw payload."""
    h, conn = host
    result = h.call_tool(conn, "poisoned_feed")
    assert result["injection_risk"] is True
    assert "ignore previous instructions" not in result["context_block"].lower()
    assert f"[connector_result_stored:{result['stored_id']}]" in result["context_block"]
    raw = conn.execute("SELECT raw_text FROM mcp_results WHERE id=?",
                       (result["stored_id"],)).fetchone()[0]
    assert "ignore previous instructions" in raw  # stored for audit, excluded from context


# ── Layer 4: workspace isolation ─────────────────────────────────────────────

def test_cred_isolation_across_workspaces(heydey_home, monkeypatch):
    """Contract C test_cred_isolation: workspace A's connector registration and
    credential are invisible to workspace B — structurally (separate db files)
    and by per-workspace Keychain naming (no wildcard)."""
    stored: dict[str, str] = {}
    monkeypatch.setattr(connectors.secrets_store, "set_secret",
                        lambda name, value: stored.__setitem__(name, value))
    monkeypatch.setattr(connectors.secrets_store, "get_secret",
                        lambda name, **k: stored.get(name))

    workspaces.create_workspace("client-a")
    workspaces.create_workspace("client-b")
    conn_a = workspaces.connect("client-a")
    conn_b = workspaces.connect("client-b")
    try:
        ref = connectors.register(conn_a, "client-a", "shopify", scopes="read_orders")
        connectors.store_credential("client-a", "shopify", "shpat-demo-secret")
        assert ref == "heydey.client-a.shopify"

        assert connectors.list_connectors(conn_b, "client-b") == []
        assert connectors.get(conn_b, "client-b", "shopify") is None
        assert connectors.get_credential("client-b", "shopify") is None  # different item name
        assert connectors.get_credential("client-a", "shopify") == "shpat-demo-secret"
        # a crossed call — B's db asked with A's id — finds nothing (separate file)
        assert connectors.get(conn_b, "client-a", "shopify") is None
    finally:
        conn_a.close()
        conn_b.close()


# ── Layer 3: egress gate ─────────────────────────────────────────────────────

def test_egress_local_only_blocks_cloud_with_connector_content():
    with pytest.raises(EgressError):
        egress_allows("local_only", "deepseek/deepseek-chat", uses_connector_content=True)
    # local lane fine; non-connector content fine; permissive profile fine
    egress_allows("local_only", "llama3.1:8b", uses_connector_content=True)
    egress_allows("local_only", "deepseek/deepseek-chat", uses_connector_content=False)
    egress_allows("any", "deepseek/deepseek-chat", uses_connector_content=True)


def test_profile_egress_field_round_trips(heydey_home):
    from heydey import models_config

    clinic = models_config.Profile(
        name="clinic", default=models_config.Pair("llama3.1:8b", "qwen3:8b"),
        egress="local_only")
    models_config.save_profile(clinic)
    loaded = models_config.load_profile("clinic")
    assert loaded.egress == "local_only"
    assert models_config.load_profile("local-only").egress == "any"  # default intact
