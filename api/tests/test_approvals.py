"""S4b: approvals lifecycle (§14-A5) + the prepared-action zero-write guarantee (L34)."""

import csv
import socket
import urllib.request
from pathlib import Path

import pytest

from heydey import approvals, workspaces


@pytest.fixture()
def appr_ws(heydey_home):
    workspaces.create_workspace("apprws")
    conn = workspaces.connect("apprws")
    yield "apprws", conn
    conn.close()


@pytest.fixture()
def network_trap(monkeypatch):
    """Any network entry point fired during a prepared action = hard fail (L34)."""
    def trap(*a, **k):
        raise AssertionError("NETWORK CALL during a prepared action — L34 violated")

    monkeypatch.setattr(urllib.request, "urlopen", trap)
    monkeypatch.setattr(socket, "create_connection", trap)
    monkeypatch.setattr(socket.socket, "connect", trap)


def test_deep_link_format():
    url = approvals.generate_shopify_admin_url("blueleaf-demo", 8843291001)
    assert url == "https://admin.shopify.com/store/blueleaf-demo/products/8843291001"


def test_lifecycle_pending_to_approved(appr_ws, network_trap):
    ws_id, conn = appr_ws
    approval_id = approvals.seed_demo_sku_approval(conn)
    tray = approvals.pending(conn)
    assert [t["id"] for t in tray] == [approval_id]
    assert tray[0]["payload"]["demo"] is True  # demo data is LABELED

    result = approvals.decide(conn, approval_id, "approved", workspace_id=ws_id)
    assert result["status"] == "approved"
    assert result["note"] == "no write was fired — prepared action only"
    assert approvals.pending(conn) == []  # tray drains


def test_approve_emits_artifact_deeplink_receipt(appr_ws, network_trap):
    ws_id, conn = appr_ws
    approval_id = approvals.seed_demo_sku_approval(conn)
    result = approvals.decide(conn, approval_id, "approved", workspace_id=ws_id)

    # 1. the artifact exists, is CSV, one row per SKU, deep-link per row
    artifact = Path(result["artifact"])
    assert artifact.is_file() and artifact.suffix == ".csv"
    rows = list(csv.DictReader(artifact.open()))
    assert len(rows) == 3
    assert all(r["admin_deep_link"].startswith("https://admin.shopify.com/store/demo-store/products/")
               for r in rows)

    # 2. the primary deep-link resolves to the first SKU's product id
    assert result["deep_link"].endswith("/products/8843291001")
    assert len(result["deep_links"]) == 3

    # 3. the receipt row exists and names the artifact
    receipt = conn.execute(
        "SELECT claim_text, doc_id, confidence_band FROM receipts WHERE run_id = ?",
        (f"approval-{approval_id}",),
    ).fetchone()
    assert receipt is not None
    assert "no write fired" in receipt[0] and receipt[2] == "action"
    assert receipt[1] == str(artifact)


def test_double_decide_refused(appr_ws, network_trap):
    ws_id, conn = appr_ws
    approval_id = approvals.seed_demo_sku_approval(conn)
    approvals.decide(conn, approval_id, "approved", workspace_id=ws_id)
    with pytest.raises(approvals.ApprovalError, match="single-shot"):
        approvals.decide(conn, approval_id, "approved", workspace_id=ws_id)


def test_deny_executes_nothing(appr_ws, network_trap):
    ws_id, conn = appr_ws
    approval_id = approvals.seed_demo_sku_approval(conn)
    result = approvals.decide(conn, approval_id, "denied", workspace_id=ws_id)
    assert result["status"] == "denied" and "artifact" not in result
    assert conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE run_id = ?", (f"approval-{approval_id}",)
    ).fetchone()[0] == 0
    artifacts_dir = approvals.config.workspaces_root() / ws_id / "artifacts"
    assert not artifacts_dir.exists() or not list(artifacts_dir.iterdir())


def test_unknown_and_invalid_paths(appr_ws):
    ws_id, conn = appr_ws
    with pytest.raises(approvals.ApprovalError, match="not found"):
        approvals.decide(conn, 999, "approved", workspace_id=ws_id)
    approval_id = approvals.create_approval(
        conn, action_class="outbound", payload={"kind": "unknown-kind"}
    )
    with pytest.raises(approvals.ApprovalError, match="decision must be"):
        approvals.decide(conn, approval_id, "maybe", workspace_id=ws_id)
    with pytest.raises(approvals.ApprovalError, match="no prepared-action executor"):
        approvals.decide(conn, approval_id, "approved", workspace_id=ws_id)


def test_approvals_module_imports_no_http_client():
    """Structural L34: nothing in approvals.py can speak to a network."""
    import ast

    import heydey.approvals as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    banned = {"urllib", "http", "requests", "httpx", "socket", "aiohttp"}
    hits = imported & banned
    assert not hits, f"{hits} imported in approvals.py — L34 violated"
