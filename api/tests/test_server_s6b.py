"""S6b: /connectors (Live Map + known servers), /connectors/register,
/connectors/sync over HTTP.

The sync endpoint spawns the REAL demo MCP subprocess (exactly as the sync loop
does) so the route is proven end to end; embeddings are stubbed to a fixed vector
so the store stays fast + offline. The full security floor (quarantine, per-tool
isolation, workspace isolation) is proven by test_connector_sync + the s6b gate —
here we prove the HTTP contract: auth, the 422 gate, the report shape, and that
the Live Map counts the freshly-synced rows the UI will render."""

import pytest

from heydey import connectors, vector_store, workspaces

from conftest import auth_headers

DIM = 384


@pytest.fixture()
def s6b_ws(heydey_home, client, monkeypatch):
    """A workspace + a stubbed embedder (no fastembed download) for sync tests."""
    workspaces.create_workspace("s6bws")
    vec = [0.0] * DIM
    vec[0] = 1.0
    monkeypatch.setattr(vector_store, "embed_texts", lambda texts: [vec for _ in texts])
    return "s6bws"


# ── auth: both POSTs are bearer-gated by the mutation middleware ──────────────

def test_register_requires_token(client, s6b_ws):
    resp = client.post("/connectors/register",
                       json={"workspace": s6b_ws, "connector_id": "demo-shopify"})
    assert resp.status_code == 401


def test_sync_requires_token(client, s6b_ws):
    resp = client.post("/connectors/sync",
                       json={"workspace": s6b_ws, "connector_id": "demo-shopify"})
    assert resp.status_code == 401


def test_connectors_get_is_open(client, s6b_ws):
    # a GET carries no mutation, so the Live Map read needs no token (like /today)
    resp = client.get(f"/connectors?workspace={s6b_ws}")
    assert resp.status_code == 200
    assert set(resp.json()["known"]) >= {"demo-shopify", "demo-sheets"}


# ── the 422 gate: connector_id must be a KNOWN_SERVERS key ────────────────────

def test_register_unknown_connector_422(client, s6b_ws):
    resp = client.post("/connectors/register",
                       json={"workspace": s6b_ws, "connector_id": "not-a-real-server"},
                       headers=auth_headers())
    assert resp.status_code == 422


def test_sync_unknown_connector_422(client, s6b_ws):
    resp = client.post("/connectors/sync",
                       json={"workspace": s6b_ws, "connector_id": "not-a-real-server"},
                       headers=auth_headers())
    assert resp.status_code == 422


def test_unknown_connector_422_beats_missing_workspace(client, heydey_home):
    # request-shape (unknown connector) is checked before the workspace resource
    resp = client.post("/connectors/register",
                       json={"workspace": "no-such-ws", "connector_id": "nope"},
                       headers=auth_headers())
    assert resp.status_code == 422


def test_known_connector_missing_workspace_404(client, heydey_home):
    resp = client.post("/connectors/register",
                       json={"workspace": "no-such-ws", "connector_id": "demo-shopify"},
                       headers=auth_headers())
    assert resp.status_code == 404


# ── register: lands the connector in the Live Map's empty (unsynced) state ────

def test_register_shows_empty_state(client, s6b_ws):
    reg = client.post("/connectors/register",
                      json={"workspace": s6b_ws, "connector_id": "demo-shopify"},
                      headers=auth_headers())
    assert reg.status_code == 200
    assert reg.json()["registered"] == "demo-shopify"

    body = client.get(f"/connectors?workspace={s6b_ws}").json()
    assert set(body["known"]) >= {"demo-shopify", "demo-sheets"}
    rows = {r["connector_id"]: r for r in body["connectors"]}
    assert "demo-shopify" in rows
    row = rows["demo-shopify"]
    assert row["results"] == 0 and row["chunks"] == 0 and row["last_sync"] is None


# ── sync: report shape + the Live Map reflects the freshly-synced counts ──────

def test_sync_demo_shopify_report_and_live_map(client, s6b_ws):
    resp = client.post("/connectors/sync",
                       json={"workspace": s6b_ws, "connector_id": "demo-shopify"},
                       headers=auth_headers())
    assert resp.status_code == 200
    report = resp.json()
    # exact report shape — every key connector_sync.sync returns, nothing more
    assert set(report) == {"connector_id", "tools_pulled", "chunks", "flagged",
                           "entities", "synced_at"}
    assert report["connector_id"] == "demo-shopify"
    # list_orders, order_stats, poisoned_feed are pull; create_discount is write
    assert report["tools_pulled"] == 3
    assert report["flagged"] == 1              # poisoned_feed quarantined + made visible
    assert report["chunks"] > 0
    assert isinstance(report["entities"], int)  # 0 for synthetic rows is fine
    assert report["synced_at"]

    # /connectors now surfaces the synced counts (the Live Map the page renders)
    body = client.get(f"/connectors?workspace={s6b_ws}").json()
    row = {r["connector_id"]: r for r in body["connectors"]}["demo-shopify"]
    assert row["results"] == 3                 # 3 pull tools -> 3 audit rows
    assert row["flagged"] == 1                 # the injection guard made visible
    assert row["chunks"] == report["chunks"]
    assert row["last_sync"] is not None


def test_sync_registers_if_needed(client, s6b_ws):
    # no explicit register first — sync must make the connector appear on the map
    resp = client.post("/connectors/sync",
                       json={"workspace": s6b_ws, "connector_id": "demo-sheets"},
                       headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["flagged"] == 0  # demo-sheets is clean

    conn = workspaces.connect(s6b_ws)
    try:
        assert connectors.get(conn, s6b_ws, "demo-sheets") is not None
    finally:
        conn.close()
    body = client.get(f"/connectors?workspace={s6b_ws}").json()
    assert "demo-sheets" in {c["connector_id"] for c in body["connectors"]}
