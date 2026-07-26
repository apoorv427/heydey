"""S6b §A — the connector sync pull loop over REAL demo MCP subprocesses.

demo-shopify / demo-sheets are spawned as actual JSON-RPC stdio servers (the S6a
transport), so these tests exercise the whole pipeline, not a stub. Embeddings
are monkeypatched to a deterministic vector so the suite stays fast + offline;
the corpus footprint (counts, payload tags, quarantine) is what's under test,
not the vector values. All data synthetic (§14-C5)."""

import json

import pytest

from heydey import connector_sync, connectors, mcp_host, vector_store, workspaces
from heydey.connector_sync import KNOWN_SERVERS, live_map, sync

DIM = 384
SHOP = KNOWN_SERVERS["demo-shopify"]
SHEETS = KNOWN_SERVERS["demo-sheets"]


@pytest.fixture(autouse=True)
def _fast_embed(monkeypatch):
    """No fastembed model load — a fixed unit vector keeps store_chunks fast."""
    vec = [0.0] * DIM
    vec[0] = 1.0
    monkeypatch.setattr(vector_store, "embed_texts", lambda texts: [vec for _ in texts])


@pytest.fixture()
def conn(heydey_home):
    workspaces.create_workspace("syncws")
    c = workspaces.connect("syncws")
    yield c
    c.close()


def _connector_points(conn, connector_id):
    return conn.execute(
        "SELECT COUNT(*) FROM points WHERE json_extract(payload,'$.connector_id') = ?",
        (connector_id,),
    ).fetchone()[0]


def _doc_points(conn, doc_id):
    return conn.execute(
        "SELECT COUNT(*) FROM points WHERE doc_id = ?", (doc_id,)
    ).fetchone()[0]


# 1 — sync stores chunks tagged as connector-sourced ─────────────────
def test_sync_stores_tagged_chunks(conn):
    report = sync(conn, "syncws", "demo-shopify", SHOP)
    assert report["connector_id"] == "demo-shopify"
    assert report["chunks"] > 0
    # list_orders, order_stats, poisoned_feed are pull; create_discount is outbound
    assert report["tools_pulled"] == 3
    assert report["chunks"] == _connector_points(conn, "demo-shopify")

    rows = conn.execute(
        "SELECT payload FROM points WHERE json_extract(payload,'$.connector_id')='demo-shopify'"
    ).fetchall()
    assert rows
    for (raw,) in rows:  # every stored connector point carries BOTH provenance tags
        payload = json.loads(raw)
        assert payload["source_type"] == "connector"
        assert payload["connector_id"] == "demo-shopify"
        assert payload["doc_id"].startswith("connector:demo-shopify:")


# 2 — re-sync is idempotent: chunks don't grow, the mcp_results audit does ─────
def test_resync_idempotent(conn):
    first = sync(conn, "syncws", "demo-shopify", SHOP)
    before = _connector_points(conn, "demo-shopify")
    second = sync(conn, "syncws", "demo-shopify", SHOP)
    after = _connector_points(conn, "demo-shopify")
    assert before == after  # deterministic ids + delete_document -> no growth
    assert first["chunks"] == second["chunks"] == after
    # the audit trail DOES grow — every pull is its own receipt row
    results = conn.execute(
        "SELECT COUNT(*) FROM mcp_results WHERE connector_id='syncws.demo-shopify'"
    ).fetchone()[0]
    assert results == 6  # 3 pull tools x 2 syncs


# 3 — the poisoned tool is counted flagged, its text never enters the corpus ──
def test_poisoned_flagged_and_excluded(conn):
    report = sync(conn, "syncws", "demo-shopify", SHOP)
    assert report["flagged"] == 1
    mark = "ignore previous instructions"
    in_results = conn.execute(
        "SELECT COUNT(*) FROM mcp_results WHERE raw_text LIKE ?", (f"%{mark}%",)
    ).fetchone()[0]
    in_points = conn.execute(
        "SELECT COUNT(*) FROM points WHERE text LIKE ? OR payload LIKE ?",
        (f"%{mark}%", f"%{mark}%"),
    ).fetchone()[0]
    assert in_results == 1  # stored for audit
    assert in_points == 0   # excluded from the retrievable corpus
    # the poisoned_feed tool was never chunked at all, but clean tools still landed
    assert _doc_points(conn, "connector:demo-shopify:poisoned_feed") == 0
    assert _connector_points(conn, "demo-shopify") > 0


# 4 — live_map counts reconcile with the tables, for both servers ────
def test_live_map_counts_match(conn):
    sync(conn, "syncws", "demo-shopify", SHOP)
    sync(conn, "syncws", "demo-sheets", SHEETS)
    rows = {r["connector_id"]: r for r in live_map(conn, "syncws")}
    assert set(rows) == {"demo-shopify", "demo-sheets"}
    for cid in ("demo-shopify", "demo-sheets"):
        row = rows[cid]
        db_results = conn.execute(
            "SELECT COUNT(*) FROM mcp_results WHERE connector_id = ?", (f"syncws.{cid}",)
        ).fetchone()[0]
        assert row["results"] == db_results
        assert row["chunks"] == _connector_points(conn, cid)
        assert row["last_sync"] is not None
    assert rows["demo-shopify"]["flagged"] == 1  # poisoned_feed made visible
    assert rows["demo-sheets"]["flagged"] == 0
    # a registered-but-unsynced connector shows the empty state (no results/chunks)
    connectors.register(conn, "syncws", "demo-idle")
    idle = {r["connector_id"]: r for r in live_map(conn, "syncws")}["demo-idle"]
    assert idle["results"] == 0 and idle["chunks"] == 0 and idle["last_sync"] is None


# 5 — one failing tool is isolated: the others still sync (Contract B) ─────────
def test_per_tool_failure_isolated(conn, monkeypatch):
    real_call = mcp_host.MCPHost.call_tool

    def flaky(self, conn_, name, *args, **kwargs):
        if name == "order_stats":
            raise RuntimeError("boom — simulated tool failure")
        return real_call(self, conn_, name, *args, **kwargs)

    monkeypatch.setattr(mcp_host.MCPHost, "call_tool", flaky)
    report = sync(conn, "syncws", "demo-shopify", SHOP)

    # list_orders still produced corpus despite order_stats blowing up
    assert report["chunks"] > 0
    assert _doc_points(conn, "connector:demo-shopify:list_orders") > 0
    # the failing tool contributed nothing, and the sync did not abort
    assert _doc_points(conn, "connector:demo-shopify:order_stats") == 0
    assert report["tools_pulled"] == 2  # list_orders + poisoned_feed; order_stats raised


# 6 — demo-sheets syncs clean (no flags) and lands ad-spend rows in corpus ────
def test_demo_sheets_syncs_clean(conn):
    report = sync(conn, "syncws", "demo-sheets", SHEETS)
    assert report["flagged"] == 0
    assert report["tools_pulled"] == 2  # list_adspend + sheet_stats, both pull-classed
    assert report["chunks"] > 0 == report["flagged"]
    corpus = conn.execute(
        "SELECT text FROM points WHERE json_extract(payload,'$.connector_id')='demo-sheets'"
    ).fetchall()
    joined = "\n".join(t for (t,) in corpus)
    assert "meta" in joined and "google" in joined  # channels reached the corpus
