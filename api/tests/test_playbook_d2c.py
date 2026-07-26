"""S6b §C: the D2C ops Playbook — RTO stats, the Morning-Brief section, and the
SKU-suppression approval, all built ONLY from clean guarded connector rows.

Test data is inserted into ``mcp_results`` directly (no dependency on the parallel
connector_sync builder). The rows mimic exactly what the MCP host stores: the
``{workspace}.{connector}`` id key, ``injection_risk`` as the quarantine flag, and
``raw_text`` = the JSON the demo-shopify ``list_orders`` tool returns."""

import csv
import json
import socket
import urllib.request
from pathlib import Path

import pytest

from heydey import approvals, morning_brief, playbook_d2c, workspaces

# 10 orders → base RTO 40.0%. Per-SKU rates are distinct + round so the assertions
# pin computed numbers, never hardcoded ones:
#   HD-A: 4 orders, 3 rto -> 75.0% | HD-B: 4 orders, 1 rto -> 25.0% | HD-C: 2, 0 -> 0.0%
ORDERS = (
    [{"order": f"A-{i}", "sku": "HD-A", "status": "rto"} for i in range(3)]
    + [{"order": "A-3", "sku": "HD-A", "status": "delivered"}]
    + [{"order": "B-0", "sku": "HD-B", "status": "rto"}]
    + [{"order": f"B-{i}", "sku": "HD-B", "status": "delivered"} for i in range(1, 4)]
    + [{"order": f"C-{i}", "sku": "HD-C", "status": "delivered"} for i in range(2)]
)


@pytest.fixture()
def d2c_ws(heydey_home):
    workspaces.create_workspace("d2cws")
    conn = workspaces.connect("d2cws")
    yield "d2cws", conn
    conn.close()


@pytest.fixture()
def network_trap(monkeypatch):
    """Any network entry point during the prepared action = hard fail (L34)."""
    def trap(*a, **k):
        raise AssertionError("NETWORK CALL during a prepared action — L34 violated")

    monkeypatch.setattr(urllib.request, "urlopen", trap)
    monkeypatch.setattr(socket, "create_connection", trap)
    monkeypatch.setattr(socket.socket, "connect", trap)


def _seed_capture(conn, raw_text, *, connector_id="d2cws.demo-shopify",
                  tool="list_orders", injection_risk=0,
                  received_at="2026-07-20T09:00:00+00:00"):
    """Insert one mcp_results row exactly as the guard would store it."""
    conn.execute(
        "INSERT INTO mcp_results(connector_id, tool, received_at, raw_text,"
        " pii_redacted, injection_risk, injection_patterns, sanitized_summary)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (connector_id, tool, received_at, raw_text, 0, injection_risk, "[]", None),
    )
    conn.commit()


def _seed_orders(conn, orders, **kw):
    _seed_capture(conn, json.dumps(orders), **kw)


# ── rto_stats ────────────────────────────────────────────────────────────────

def test_rto_stats_computed_from_seeded_row(d2c_ws):
    _, conn = d2c_ws
    _seed_orders(conn, ORDERS)
    stats = playbook_d2c.rto_stats(conn, "d2cws")
    assert stats["orders"] == 10
    assert stats["rto"] == 4
    assert stats["rto_rate_pct"] == 40.0
    assert stats["base_rate_pct"] == 40.0
    assert [s["sku"] for s in stats["by_sku"]] == ["HD-A", "HD-B", "HD-C"]  # desc by rate
    assert stats["by_sku"][0] == {"sku": "HD-A", "orders": 4, "rto": 3, "rto_rate_pct": 75.0}
    assert stats["by_sku"][1]["rto_rate_pct"] == 25.0
    assert stats["by_sku"][2]["rto_rate_pct"] == 0.0


def test_rto_stats_none_when_unsynced(d2c_ws):
    _, conn = d2c_ws
    assert playbook_d2c.rto_stats(conn, "d2cws") is None


def test_latest_clean_capture_supersedes_older(d2c_ws):
    _, conn = d2c_ws
    _seed_orders(conn, [{"order": "o", "sku": "OLD", "status": "delivered"}],
                 received_at="2026-07-19T09:00:00+00:00")
    _seed_orders(conn, ORDERS, received_at="2026-07-20T09:00:00+00:00")  # newer, higher id
    assert playbook_d2c.rto_stats(conn, "d2cws")["orders"] == 10


def test_malformed_or_wrong_shape_captures_yield_none(d2c_ws):
    _, conn = d2c_ws
    # not JSON at all
    _seed_capture(conn, "not json at all {")
    assert playbook_d2c.rto_stats(conn, "d2cws") is None
    # valid JSON but the wrong shape (a dict, not a list of orders)
    _seed_capture(conn, json.dumps({"orders": 5}))
    assert playbook_d2c.rto_stats(conn, "d2cws") is None
    # a list of orders missing the sku key
    _seed_capture(conn, json.dumps([{"order": "x", "status": "rto"}]))
    assert playbook_d2c.rto_stats(conn, "d2cws") is None


# ── brief_section ─────────────────────────────────────────────────────────────

def test_brief_section_breadcrumbed_and_computed(d2c_ws):
    _, conn = d2c_ws
    _seed_orders(conn, ORDERS, received_at="2026-07-20T09:00:00+00:00")
    items = playbook_d2c.brief_section(conn, "d2cws")
    assert len(items) >= 2
    for it in items:
        assert it["section"] == "d2c-ops"
        assert it["breadcrumb"] == {"source": "connector:demo-shopify:list_orders",
                                    "chunk": None, "date": "2026-07-20"}
    joined = " ".join(i["line"] for i in items)
    assert "40.0%" in joined and "10 orders" in joined  # overall, computed
    assert "HD-A" in joined and "75.0%" in joined and "40.0% base" in joined  # concentration


def test_brief_section_empty_when_unsynced(d2c_ws):
    _, conn = d2c_ws
    assert playbook_d2c.brief_section(conn, "d2cws") == []


def test_morning_brief_registers_and_renders_d2c_section(d2c_ws):
    """The EXTRA_SECTIONS hook: build_brief runs d2c-ops with workspace_id."""
    assert "d2c-ops" in {name for name, _ in morning_brief.EXTRA_SECTIONS}
    ws_id, conn = d2c_ws
    _seed_orders(conn, ORDERS)
    items = morning_brief.build_brief(conn, workspace_id=ws_id)
    d2c = [i for i in items if i["section"] == "d2c-ops"]
    assert len(d2c) >= 2 and any("%" in i["line"] for i in d2c)
    assert all("connector" in i["breadcrumb"]["source"] for i in d2c)


def test_morning_brief_omits_d2c_when_unsynced(d2c_ws):
    ws_id, conn = d2c_ws
    items = morning_brief.build_brief(conn, workspace_id=ws_id)
    assert [i for i in items if i["section"] == "d2c-ops"] == []


# ── suppression_approval ──────────────────────────────────────────────────────

def test_suppression_payload_carries_computed_rates(d2c_ws):
    ws_id, conn = d2c_ws
    _seed_orders(conn, ORDERS)
    approval_id = playbook_d2c.suppression_approval(conn, ws_id)
    assert approval_id is not None

    tray = approvals.pending(conn)
    assert [t["id"] for t in tray] == [approval_id]
    payload = tray[0]["payload"]
    assert payload["kind"] == "sku_suppression"  # S4b executor works as-is
    assert payload["store_handle"] == "demo-store"
    assert payload["base_return_rate"] == 40.0

    by_sku = {s["sku"]: s for s in payload["skus"]}
    assert by_sku["HD-A"]["return_rate"] == 75.0  # COMPUTED, replaces hardcoded seed
    assert by_sku["HD-B"]["return_rate"] == 25.0
    assert by_sku["HD-A"]["units_30d"] == 4  # real per-sku order count
    assert by_sku["HD-A"]["product_id"] == playbook_d2c._demo_product_id("HD-A")  # stable


def test_suppression_respects_top_n(d2c_ws):
    ws_id, conn = d2c_ws
    _seed_orders(conn, ORDERS)  # 3 distinct skus
    approval_id = playbook_d2c.suppression_approval(conn, ws_id, top_n=2)
    payload = approvals.pending(conn)[0]["payload"]
    assert [s["sku"] for s in payload["skus"]] == ["HD-A", "HD-B"]  # 2 worst by rate


def test_suppression_none_when_unsynced(d2c_ws):
    _, conn = d2c_ws
    assert playbook_d2c.suppression_approval(conn, "d2cws") is None


def test_suppression_approves_through_s4b_executor(d2c_ws, network_trap):
    """FROM real synced rows -> approve -> CSV rows match synced skus, deep-links,
    computed rates in the artifact. Proves the S4b prepared action runs unchanged."""
    ws_id, conn = d2c_ws
    _seed_orders(conn, ORDERS)
    stats = playbook_d2c.rto_stats(conn, ws_id)
    approval_id = playbook_d2c.suppression_approval(conn, ws_id)

    result = approvals.decide(conn, approval_id, "approved", workspace_id=ws_id)
    assert result["note"].startswith("no write was fired")

    rows = list(csv.DictReader(Path(result["artifact"]).open()))
    assert rows and {r["sku"] for r in rows} <= {s["sku"] for s in stats["by_sku"]}
    assert all(r["admin_deep_link"].startswith(
        "https://admin.shopify.com/store/demo-store/products/") for r in rows)
    hd_a = next(r for r in rows if r["sku"] == "HD-A")
    assert hd_a["return_rate_pct"] == "75.0" and hd_a["base_rate_pct"] == "40.0"


# ── quarantine: flagged rows never build anything ─────────────────────────────

def test_flagged_only_data_never_builds(d2c_ws):
    """A poisoned/flagged capture is the only row — stats/brief/approval all refuse
    it (WHERE injection_risk = 0). Never build from quarantined rows."""
    _, conn = d2c_ws
    _seed_orders(conn, ORDERS, injection_risk=1)
    assert playbook_d2c.rto_stats(conn, "d2cws") is None
    assert playbook_d2c.brief_section(conn, "d2cws") == []
    assert playbook_d2c.suppression_approval(conn, "d2cws") is None


def test_flagged_capture_does_not_shadow_clean_one(d2c_ws):
    """A LATER flagged row must not hide an earlier clean capture."""
    _, conn = d2c_ws
    _seed_orders(conn, ORDERS, injection_risk=0, received_at="2026-07-20T09:00:00+00:00")
    _seed_orders(conn, ORDERS, injection_risk=1, received_at="2026-07-21T09:00:00+00:00")
    assert playbook_d2c.rto_stats(conn, "d2cws")["orders"] == 10
