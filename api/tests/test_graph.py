"""Executor Contract B — the graph extraction engine (anti-LightRAG).

Event-driven, pure SQLite: entities @ ingest, edges @ co-retrieval, health signal.
The banned pattern (LLM inside a cron batch) is guarded structurally: graph.py makes
NO LLM calls in its core path."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from heydey import graph, workspaces


@pytest.fixture()
def conn(heydey_home):
    workspaces.create_workspace("graphws")
    c = workspaces.connect("graphws")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _seed_projects(monkeypatch):
    """Neutral project vocabulary — tests must never depend on the machine-local
    corpus.json graph_seed (extraction MECHANICS are under test, not real names)."""
    monkeypatch.setattr(graph, "KNOWN_PROJECTS",
                        ["ORION", "VEGA", "KESTREL", "HUBWARD", "Heydey"])


# ── test_graph_grows_on_ingest ────────────────────────────────────────────────
def test_graph_grows_on_ingest(conn):
    before = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    text = "ORION is LOCKED under L28. VEGA is the operating system. PENDING gate S3."
    n = graph.index_document(conn, "doc-a", text, "graphws")
    after = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert n >= 3
    assert after - before >= 3
    labels = {r[0] for r in conn.execute("SELECT label FROM entities").fetchall()}
    assert {"ORION", "VEGA"} <= labels


def test_ingest_is_idempotent(conn):
    text = "ORION and VEGA and Heydey."
    graph.index_document(conn, "doc-a", text, "graphws")
    first = conn.execute("SELECT COUNT(*) FROM entities WHERE source_doc_id='doc-a'").fetchone()[0]
    graph.index_document(conn, "doc-a", text, "graphws")  # re-ingest
    second = conn.execute("SELECT COUNT(*) FROM entities WHERE source_doc_id='doc-a'").fetchone()[0]
    assert first == second  # no duplication


# ── test_edge_on_coretrieval ──────────────────────────────────────────────────
def test_edge_on_coretrieval(conn):
    graph.index_document(conn, "d1", "ORION pricing is LOCKED.", "graphws")
    graph.index_document(conn, "d2", "VEGA orbit is a GATE.", "graphws")
    hits = [{"payload": {"doc_id": "d1"}}, {"payload": {"doc_id": "d2"}}]
    n = graph.record_coretrieval(conn, "run-1", hits)
    assert n >= 1
    edges = conn.execute("SELECT COUNT(*) FROM activity_edges WHERE run_id='run-1'").fetchone()[0]
    assert edges >= 1


# ── test_bad_doc_skips (per-doc isolation, pipeline continues) ─────────────────
def test_bad_doc_skips(conn):
    graph.index_document(conn, "good1", "ORION is here.", "graphws")
    baseline = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    # a malformed input must not raise or corrupt the graph
    assert graph.index_document(conn, "bad", None, "graphws") == 0  # type: ignore[arg-type]
    graph.index_document(conn, "good2", "VEGA is also here.", "graphws")
    after = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert after > baseline  # good docs after the bad one still indexed


# ── health + top_entities render data ─────────────────────────────────────────
def test_health_and_top_entities(conn):
    graph.index_document(conn, "d1", "ORION LOCKED.", "graphws")
    graph.index_document(conn, "d2", "VEGA GATE.", "graphws")
    graph.record_coretrieval(conn, "r1", [{"payload": {"doc_id": "d1"}}, {"payload": {"doc_id": "d2"}}])
    h = graph.health(conn)
    assert h["entities"] >= 2 and h["edges"] >= 1
    top = graph.top_entities(conn, limit=10)
    assert top and "label" in top[0]


# ── the banned pattern is structurally impossible: graph.py has NO LLM import ──
def test_graph_makes_no_llm_calls():
    src = (Path(__file__).resolve().parents[1] / "heydey" / "graph.py").read_text()
    assert "llm_client" not in src
    assert "import" in src and "requests" not in src  # no network client at all


def test_reingest_preserves_entity_ids_and_edges(conn):
    """Regression (S4c, found live): delete+reinsert on re-ingest minted new entity
    ids and orphaned EVERY activity_edge (1,344 dead). Stable labels must keep
    their ids across re-ingests, and their edge history must stay attached."""
    ws_id = "graphws"
    text = "ORION quote LOCKED at L23. Heydey S3 GATE green."
    graph.index_document(conn, "doc-stable", text, ws_id)
    ids_before = {r[1]: r[0] for r in conn.execute(
        "SELECT id, label FROM entities WHERE source_doc_id='doc-stable'")}

    hits = [{"payload": {"doc_id": "doc-stable"}}, {"payload": {"doc_id": "doc-stable2"}}]
    graph.index_document(conn, "doc-stable2", "VEGA orbit PENDING review.", ws_id)
    graph.record_coretrieval(conn, "run-keep", hits)
    edges_before = conn.execute("SELECT COUNT(*) FROM activity_edges").fetchone()[0]
    assert edges_before > 0

    # re-ingest the SAME doc (content unchanged) — ids and edges must survive
    graph.index_document(conn, "doc-stable", text, ws_id)
    ids_after = {r[1]: r[0] for r in conn.execute(
        "SELECT id, label FROM entities WHERE source_doc_id='doc-stable'")}
    assert ids_after == ids_before, "stable labels must keep their entity ids"
    live = conn.execute(
        "SELECT COUNT(*) FROM activity_edges ae"
        " WHERE EXISTS (SELECT 1 FROM entities e WHERE e.id=ae.entity_a)"
        "   AND EXISTS (SELECT 1 FROM entities e WHERE e.id=ae.entity_b)").fetchone()[0]
    assert live == edges_before, "edge history must survive a re-ingest"


def test_removed_entity_takes_its_edges_along(conn):
    """An entity that disappears from a doc is deleted WITH its edges — no dangling
    endpoints, so the panel can never render an <unknown> node."""
    ws_id = "graphws"
    graph.index_document(conn, "doc-x", "ORION and KESTREL PENDING.", ws_id)
    graph.index_document(conn, "doc-y", "HUBWARD deck LOCKED.", ws_id)
    graph.record_coretrieval(conn, "run-x", [
        {"payload": {"doc_id": "doc-x"}}, {"payload": {"doc_id": "doc-y"}}])
    assert conn.execute("SELECT COUNT(*) FROM activity_edges").fetchone()[0] > 0

    # re-ingest doc-x with ALL its entities gone
    graph.index_document(conn, "doc-x", "nothing notable here anymore", ws_id)
    dangling = conn.execute(
        "SELECT COUNT(*) FROM activity_edges ae"
        " WHERE NOT EXISTS (SELECT 1 FROM entities e WHERE e.id=ae.entity_a)"
        "    OR NOT EXISTS (SELECT 1 FROM entities e WHERE e.id=ae.entity_b)").fetchone()[0]
    assert dangling == 0, "edges of removed entities must be purged at the source"


def test_panel_returns_live_nodes_and_weighted_edges(conn):
    ws_id = "graphws"
    graph.index_document(conn, "d1", "ORION pricing LOCKED L23.", ws_id)
    graph.index_document(conn, "d2", "Heydey S3 GATE green for ORION.", ws_id)
    hits = [{"payload": {"doc_id": "d1"}}, {"payload": {"doc_id": "d2"}}]
    graph.record_coretrieval(conn, "run-1", hits)
    graph.record_coretrieval(conn, "run-2", hits)  # same pair again -> weight 2

    payload = graph.panel(conn, limit=50)
    assert payload["nodes"] and payload["edges"]
    assert all(n["label"] != "<unknown>" for n in payload["nodes"])
    assert payload["edges"][0]["weight"] >= 2
    node_ids = {n["id"] for n in payload["nodes"]}
    assert all(e["source"] in node_ids and e["target"] in node_ids for e in payload["edges"])


def test_entity_detail_carries_receipts_and_sessions(conn):
    ws_id = "graphws"
    graph.index_document(conn, "d-det", "ORION GATE pending.", ws_id)
    graph.index_document(conn, "d-det2", "KESTREL LOCKED.", ws_id)
    graph.record_coretrieval(conn, "run-det", [
        {"payload": {"doc_id": "d-det"}}, {"payload": {"doc_id": "d-det2"}}])
    conn.execute(
        "INSERT INTO sessions(id, intent, started_at, duration, cost) "
        "VALUES ('run-det', 'orion gate check', '2026-07-19T10:00:00', 1.0, 0.0)")
    conn.execute(
        "INSERT INTO receipts(run_id, sentence_index, claim_text, doc_id, created_at) "
        "VALUES ('run-det', 0, 'ORION gate is pending', 'd-det', '2026-07-19T10:00:01')")
    conn.commit()

    eid = conn.execute(
        "SELECT id FROM entities WHERE label='ORION' AND source_doc_id='d-det'"
    ).fetchone()[0]
    detail = graph.entity_detail(conn, eid)
    assert detail["label"] == "ORION"
    assert "run-det" in detail["runs"]
    assert detail["sessions"] and detail["sessions"][0]["intent"] == "orion gate check"
    assert detail["receipts"] and "ORION" in detail["receipts"][0]["claim_text"]
    assert graph.entity_detail(conn, 999999) is None


def test_money_labels_normalise_to_one_node():
    """'₹12 L', '₹12L' and '₹18,' were separate live nodes — money labels must
    collapse to one canonical spelling before entity identity forms."""
    ents = graph.extract_entities("quote ₹12 L, floor ₹12L, anchor ₹18, settle ₹9.")
    money = sorted(e["label"] for e in ents if e["type"] == "money")
    assert "₹12L" in money and "₹18" in money and "₹9" in money
    assert money.count("₹12L") == 1
    assert not any(l.endswith(",") or " " in l for l in money)
