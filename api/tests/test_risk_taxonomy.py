"""W2 — the 4-tier risk taxonomy (read/write_local/exec/external), wired
manifest-parse -> approvals -> receipts.

The plan's own gate: every prepared action carries a tier, `external` requires
approval, and the tier lands in the receipts row so risk class is audit trail.
"""

import sys

import pytest

from heydey import approvals, risk, workspaces
from heydey.mcp_host import ApprovalRequired, MCPHost

DEMO_CMD = [sys.executable, "-m", "heydey.demo_connector"]


@pytest.fixture()
def ws(heydey_home):
    workspaces.create_workspace("riskws")
    conn = workspaces.connect("riskws")
    yield "riskws", conn
    conn.close()


# ── the resolution rule ───────────────────────────────────────────────────────

def test_infer_shapes():
    assert risk.infer_tier("list_orders") == "read"
    assert risk.infer_tier("send_report") == "external"
    assert risk.infer_tier("refund_order") == "external"
    assert risk.infer_tier("run_script") == "exec"
    assert risk.infer_tier("frobnicate_widget") is None  # no signal — caller fails closed


def test_resolve_riskier_of_wins():
    # a mislabeled manifest cannot downgrade: declared read + send-shape -> external
    assert risk.resolve_tier("read", "send_report") == "external"
    # declaration can raise above inference
    assert risk.resolve_tier("external", "list_things") == "external"
    assert risk.resolve_tier("write_local", "upload_file") == "external"
    # no verb signal: declaration is trusted; no declaration -> top tier
    assert risk.resolve_tier("write_local", "prepare_summary") == "write_local"
    assert risk.resolve_tier(None, "frobnicate_widget") == "external"
    assert risk.resolve_tier(None, "list_orders") == "read"


def test_invalid_tier_raises():
    with pytest.raises(risk.RiskError):
        risk.resolve_tier("banana", "list_orders")
    with pytest.raises(risk.RiskError):
        risk.requires_approval("banana")


def test_only_read_runs_free():
    assert risk.requires_approval("read") is False
    for tier in ("write_local", "exec", "external"):
        assert risk.requires_approval(tier) is True


# ── manifest-parse: MCPHost resolves tiers, declared or inferred ─────────────

def test_host_manifest_carries_tiers(ws):
    ws_id, conn = ws
    with MCPHost(workspace_id=ws_id, connector_id="demo", command=DEMO_CMD) as h:
        tiers = {t["name"]: t["risk_tier"] for t in h.list_tools()}
    assert tiers == {"list_orders": "read", "order_stats": "read",
                     "create_discount": "external", "poisoned_feed": "read"}


def test_declared_tier_cannot_downgrade_a_tool(ws):
    """A hostile/buggy manifest declaring the write tool `read` still gates."""
    ws_id, conn = ws
    declared = {"create_discount": "read"}  # lie
    with MCPHost(workspace_id=ws_id, connector_id="demo", command=DEMO_CMD,
                 declared_tiers=declared) as h:
        tiers = {t["name"]: t["risk_tier"] for t in h.list_tools()}
        assert tiers["create_discount"] == "external"  # riskier-of won
        with pytest.raises(ApprovalRequired):
            h.call_tool(conn, "create_discount", {"code": "X"})


def test_read_tool_result_carries_tier(ws):
    ws_id, conn = ws
    with MCPHost(workspace_id=ws_id, connector_id="demo", command=DEMO_CMD) as h:
        result = h.call_tool(conn, "list_orders")
    assert result["risk_tier"] == "read"


# ── approvals: every prepared action carries a tier ──────────────────────────

def test_approval_defaults_to_top_tier(ws):
    _, conn = ws
    approval_id = approvals.create_approval(
        conn, action_class="outbound", payload={"kind": "connector_call"})
    row = approvals.pending(conn)[0]
    assert row["id"] == approval_id and row["risk_tier"] == "external"


def test_approval_rejects_unknown_tier(ws):
    _, conn = ws
    with pytest.raises(risk.RiskError):
        approvals.create_approval(conn, action_class="outbound",
                                  payload={"kind": "x"}, risk_tier="banana")


def test_sku_prepared_action_is_write_local_and_tier_reaches_receipt(ws):
    ws_id, conn = ws
    approval_id = approvals.seed_demo_sku_approval(conn)
    assert approvals.pending(conn)[0]["risk_tier"] == "write_local"

    result = approvals.decide(conn, approval_id, "approved", workspace_id=ws_id)
    assert result["risk_tier"] == "write_local"
    tier, claim = conn.execute(
        "SELECT risk_tier, claim_text FROM receipts WHERE run_id = ?",
        (f"approval-{approval_id}",)).fetchone()
    assert tier == "write_local"
    assert "risk write_local" in claim


def test_pre_taxonomy_row_decides_at_top_tier(ws):
    """A NULL-tier row (pre-W2 db) resolves external on decide — fail closed."""
    ws_id, conn = ws
    approval_id = approvals.create_approval(
        conn, action_class="outbound",
        payload={"kind": "connector_call", "title": "legacy"})
    conn.execute("UPDATE approvals SET risk_tier = NULL WHERE id = ?", (approval_id,))
    conn.commit()
    result = approvals.decide(conn, approval_id, "approved", workspace_id=ws_id)
    assert result["risk_tier"] == "external"
