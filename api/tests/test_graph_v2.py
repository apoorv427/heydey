"""Graph v2 (G1 rebuild) — falsifiable proofs for the five measured failures.

Each test below fails on the v1 graph and passes on the rebuild:

1. identity      — one node per real-world thing (v1: 26 "ORION" nodes)
2. precision     — function words / months / OS folders never become nodes
                   (v1: 3,319 entities at the 0.55 floor, 307 pure function words)
3. money         — one node per amount, no unit-less fragments
4. typed edges   — predicate validated, provenance mandatory (v1: 0 relationships)
5. hub bias      — PMI scoring, so a token that co-occurs with everything ranks 0
plus per-document isolation, which the whole ingest path depends on.
"""

import pytest

from heydey import graph, graph_resolve, workspaces


@pytest.fixture()
def conn(heydey_home):
    workspaces.create_workspace("gws2")
    c = workspaces.connect("gws2")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _seed_projects(monkeypatch):
    """Neutral vocabulary — extraction MECHANICS are under test, never the
    machine-local corpus.json graph_seed."""
    monkeypatch.setattr(graph, "KNOWN_PROJECTS", ["ORION", "VEGA", "HUB", "ALPHA", "BETA"])


# ── 1. identity: one entity, two aliases, two mentions ───────────────────────

def test_possessive_and_repeat_resolve_to_one_entity(conn):
    """v1 keyed on (label, type, source_doc_id): this made TWO nodes, and the
    possessive form a third. One canonical key -> one node."""
    graph.index_document(conn, "d1", "# Ravensbourne\n\nRavensbourne shipped it.", "gws2")
    graph.index_document(
        conn, "d2", "Ravensbourne's roadmap slipped. Ravensbourne owns the fix.", "gws2")

    rows = conn.execute(
        "SELECT id, label, mention_count FROM graph_entities WHERE canonical_key='ravensbourne'"
    ).fetchall()
    assert len(rows) == 1, "one real-world thing must be exactly one node"
    entity_id, label, mentions = rows[0]
    assert label == "Ravensbourne"          # canonical display, possessive stripped
    assert mentions == 2                    # one per document

    aliases = {r[0] for r in conn.execute(
        "SELECT alias_key FROM graph_aliases WHERE entity_id=?", (entity_id,))}
    assert aliases == {"ravensbourne", "ravensbourne's"}

    docs = {r[0] for r in conn.execute(
        "SELECT doc_id FROM graph_mentions WHERE entity_id=?", (entity_id,))}
    assert docs == {"d1", "d2"}


def test_fuzzy_merge_inside_type_only(conn):
    near = graph_resolve.resolve_entity(conn, "Ravensbourne", "org", "gws2",
                                        confidence=0.9, doc_id="d1")
    typo = graph_resolve.resolve_entity(conn, "Ravensbourn", "org", "gws2",
                                        confidence=0.9, doc_id="d2")
    assert typo == near, "difflib ratio >= 0.92 inside one type merges"

    other_type = graph_resolve.resolve_entity(conn, "Ravensbourne", "product", "gws2",
                                              confidence=0.9, doc_id="d3")
    assert other_type != near, "identity never merges across types"

    # short keys are never fuzzy-merged (L23 must not swallow L28)
    lock_a = graph_resolve.resolve_entity(conn, "L23", "lock", "gws2",
                                          confidence=0.9, doc_id="d1")
    lock_b = graph_resolve.resolve_entity(conn, "L28", "lock", "gws2",
                                          confidence=0.9, doc_id="d1")
    assert lock_a != lock_b


# ── 2. precision: the junk class is gone ─────────────────────────────────────

JUNK = ["How", "Desktop", "Downloads", "Documents", "Jul", "August", "Monday",
        "Two", "Why", "Open", "Status", "Owner", "Next", "Note", "Library",
        "Applications", "Screenshots", "Version", "Summary", "Personal"]


def test_function_words_months_and_folders_never_become_entities(conn):
    body = "\n".join(f"{word} matters here. {word} again below." for word in JUNK)
    text = f"# Kestrel Systems\n\nKestrel Systems shipped the release.\n\n{body}"

    labels = {e["label"] for e in graph.extract_entities(text)}
    assert not (labels & set(JUNK)), f"junk survived extraction: {labels & set(JUNK)}"

    graph.index_document(conn, "junk-doc", text, "gws2")
    stored = {r[0] for r in conn.execute("SELECT label FROM graph_entities")}
    assert not (stored & set(JUNK)), f"junk became nodes: {stored & set(JUNK)}"
    # ...and the filter is selective, not a blanket reject
    assert "Kestrel Systems" in stored


def test_single_sighting_proper_noun_is_gated_out(conn):
    """A capitalised run seen once, off a heading, is not evidence of anything."""
    once = graph.extract_entities("The quote came from Wexford Partners last week.")
    assert "Wexford Partners" not in {e["label"] for e in once}

    twice = graph.extract_entities(
        "Wexford Partners sent the quote. Wexford Partners will confirm.")
    assert "Wexford Partners" in {e["label"] for e in twice}


# ── 3. money: one node per amount, no unit-less fragments ────────────────────

def test_money_spellings_collapse_and_fragments_are_not_nodes(conn):
    text = ("Deal booked at ₹35.4L; the invoice reads ₹35,40,000 and the term "
            "sheet says ₹35.4 lakh. Coffee cost ₹25.")

    graph.index_document(conn, "money-doc", text, "gws2")
    money = conn.execute(
        "SELECT label, canonical_key FROM graph_entities WHERE type='money'").fetchall()
    assert len(money) == 1, f"three spellings of one amount must be one node: {money}"
    assert money[0][0] == "₹35.4L"
    assert money[0][1] == "inr:3540000.00"

    # the bare fragment is SEEN (honest candidate) but sits below the node gate
    candidates = {e["label"]: e["confidence"]
                  for e in graph.extract_entities(text) if e["type"] == "money"}
    assert candidates["₹25"] < 0.55
    assert not conn.execute(
        "SELECT 1 FROM graph_entities WHERE label='₹25'").fetchone()


def test_money_parser_arithmetic():
    assert graph_resolve.parse_money("₹35.4L")["value"] == pytest.approx(3_540_000)
    assert graph_resolve.parse_money("₹35,40,000")["value"] == pytest.approx(3_540_000)
    assert graph_resolve.parse_money("₹35.4 lakh")["value"] == pytest.approx(3_540_000)
    assert graph_resolve.parse_money("₹2 Cr")["label"] == "₹2Cr"
    assert graph_resolve.parse_money("₹25")["qualifies"] is False
    assert graph_resolve.parse_money("₹1,500")["qualifies"] is True
    assert graph_resolve.parse_money("no money here") is None


# ── 4. typed edges: validated predicate + mandatory provenance ───────────────

def test_add_edge_refuses_unknown_predicate_and_missing_provenance(conn):
    src = graph_resolve.resolve_entity(conn, "Alpha Corp", "org", "gws2",
                                       confidence=0.9, doc_id="d1")
    dst = graph_resolve.resolve_entity(conn, "Beta Ltd", "org", "gws2",
                                       confidence=0.9, doc_id="d1")

    with pytest.raises(ValueError):
        graph.add_edge(conn, src, dst, "FRIENDS_WITH", confidence=0.9,
                       doc_id="d1", extractor="regex")
    with pytest.raises(ValueError):   # cite-or-silent: no doc_id, no edge
        graph.add_edge(conn, src, dst, "CLIENT_OF", confidence=0.9,
                       doc_id="", extractor="regex")
    with pytest.raises(ValueError):
        graph.add_edge(conn, src, dst, "CLIENT_OF", confidence=0.9,
                       doc_id="d1", extractor="handwave")

    assert graph.add_edge(conn, src, dst, "CLIENT_OF", confidence=0.9,
                          doc_id="d1", chunk_id="c1", extractor="regex") is True
    assert graph.add_edge(conn, src, dst, "CLIENT_OF", confidence=0.9,
                          doc_id="d1", chunk_id="c1", extractor="regex") is False

    row = conn.execute(
        "SELECT predicate, doc_id, chunk_id, extractor, weight FROM graph_edges").fetchone()
    assert row[0] == "CLIENT_OF" and row[1] == "d1" and row[2] == "c1"
    assert row[3] == "regex" and row[4] == 2.0     # re-assertion bumps weight
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_edges WHERE doc_id IS NULL OR doc_id=''"
    ).fetchone()[0] == 0


def test_every_edge_written_by_ingest_carries_provenance(conn):
    graph.index_document(conn, "prov-doc", "ORION and VEGA and HUB ship together.", "gws2")
    rows = conn.execute(
        "SELECT doc_id, extractor, predicate FROM graph_edges").fetchall()
    assert rows
    for doc_id, extractor, predicate in rows:
        assert doc_id, "an edge nobody can source is not shippable"
        assert extractor in graph.EXTRACTORS
        assert predicate in graph.PREDICATES


# ── 5. traversal: a 2-hop typed path, every hop sourced ──────────────────────

def test_two_hop_neighbors_returns_typed_sourced_path(conn):
    a = graph_resolve.resolve_entity(conn, "Alpha Corp", "org", "gws2",
                                     confidence=0.9, doc_id="d1")
    b = graph_resolve.resolve_entity(conn, "Beta Ltd", "org", "gws2",
                                     confidence=0.9, doc_id="d1")
    c = graph_resolve.resolve_entity(conn, "Gamma Works", "org", "gws2",
                                     confidence=0.9, doc_id="d2")
    graph.add_edge(conn, a, b, "BLOCKS", confidence=0.8, doc_id="d1",
                   chunk_id="d1#0", extractor="regex")
    graph.add_edge(conn, b, c, "DEPENDS_ON", confidence=0.8, doc_id="d2",
                   chunk_id="d2#0", extractor="regex")

    hops = graph.neighbors(conn, a, hops=2)
    by_id = {h["id"]: h for h in hops}
    assert by_id[b]["hop"] == 1 and by_id[b]["predicate"] == "BLOCKS"
    assert by_id[c]["hop"] == 2 and by_id[c]["predicate"] == "DEPENDS_ON"
    assert by_id[c]["via"] == b
    for hop in hops:
        assert hop["doc_id"], "every hop must carry its provenance"
        assert hop["predicate"] in graph.PREDICATES

    assert a not in by_id                       # the start node is not its own neighbour
    assert graph.neighbors(conn, a, hops=1) == [h for h in hops if h["hop"] == 1]


def test_entity_profile_carries_aliases_relations_and_receipts(conn):
    graph.index_document(conn, "p1", "# Ravensbourne\n\nRavensbourne shipped ORION.", "gws2")
    conn.execute(
        "INSERT INTO receipts(run_id, sentence_index, claim_text, doc_id, created_at)"
        " VALUES ('run-p', 0, 'Ravensbourne shipped ORION', 'p1', '2026-07-27T10:00:00')")
    conn.commit()

    profile = graph.entity_profile(conn, "ravensbourne", workspace_id="gws2")
    assert profile["entity"]["label"] == "Ravensbourne"
    assert profile["entity"]["mention_count"] == 1
    assert [a["alias"] for a in profile["aliases"]] == ["Ravensbourne"]
    assert [m["doc_id"] for m in profile["mention_docs"]] == ["p1"]
    assert profile["related"] and profile["related"][0]["doc_id"] == "p1"
    assert profile["receipts"][0]["claim_text"].startswith("Ravensbourne")

    # id lookup and a miss
    same = graph.entity_profile(conn, profile["entity"]["id"])
    assert same["entity"]["id"] == profile["entity"]["id"]
    assert graph.entity_profile(conn, "no-such-entity") is None


# ── 6. hub bias dies with PMI ────────────────────────────────────────────────

def test_pmi_scores_a_hub_to_zero(conn):
    """HUB appears in all six documents; ALPHA and BETA only ever appear
    together. Raw co-occurrence count ranks HUB top (the v1 failure); PMI
    scores it 0 and lifts the specific pair."""
    graph.index_document(conn, "h0", "HUB ALPHA BETA", "gws2")
    graph.index_document(conn, "h1", "HUB ALPHA BETA", "gws2")
    for i in range(2, 6):
        graph.index_document(conn, f"h{i}", "HUB alone in this note.", "gws2")

    assert graph.mine_relationships(conn) > 0

    def weight(label_a, label_b):
        ids = {r[0]: r[1] for r in conn.execute("SELECT label, id FROM graph_entities")}
        lo, hi = sorted((ids[label_a], ids[label_b]))
        return conn.execute(
            "SELECT weight FROM graph_edges WHERE src_id=? AND dst_id=? AND extractor='pmi'",
            (lo, hi)).fetchone()[0]

    assert weight("HUB", "ALPHA") == 0.0
    assert weight("ALPHA", "BETA") > 1.0

    top = {n["label"]: n["score"] for n in graph.top_entities(conn, limit=10)}
    assert top["ALPHA"] > top["HUB"] and top["BETA"] > top["HUB"]


def test_panel_is_typed_capped_and_internally_consistent(conn):
    graph.index_document(conn, "pd1", "ORION pricing LOCKED L23.", "gws2")
    graph.index_document(conn, "pd2", "VEGA S3 GATE for ORION.", "gws2")
    hits = [{"payload": {"doc_id": "pd1"}}, {"payload": {"doc_id": "pd2"}}]
    graph.record_coretrieval(conn, "run-1", hits)
    graph.record_coretrieval(conn, "run-2", hits)

    payload = graph.panel(conn, limit=999)
    assert len(payload["nodes"]) <= 50
    node_ids = {n["id"] for n in payload["nodes"]}
    assert payload["edges"]
    for edge in payload["edges"]:
        assert edge["source"] in node_ids and edge["target"] in node_ids
        assert edge["predicate"] in graph.PREDICATES
        assert edge["doc_id"]
    assert payload["edges"][0]["weight"] >= 2      # two runs on the same pair
    assert payload["health"]["entities"] == len(
        conn.execute("SELECT id FROM graph_entities").fetchall())

    # co-retrieval provenance joins straight back to the run
    runs = {r[0] for r in conn.execute(
        "SELECT DISTINCT doc_id FROM graph_edges WHERE extractor='co_activity'")}
    assert runs == {"run:run-1", "run:run-2"}


# ── 7. per-document isolation is absolute ────────────────────────────────────

def test_index_document_is_per_document_isolated(conn, monkeypatch):
    graph.index_document(conn, "ok-1", "ORION and VEGA ship.", "gws2")
    baseline_entities = conn.execute("SELECT COUNT(*) FROM graph_entities").fetchone()[0]
    baseline_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    assert baseline_entities >= 2

    # (a) a doc that cannot even be parsed
    assert graph.index_document(conn, "bad-1", None, "gws2") == 0  # type: ignore[arg-type]

    # (b) a doc that raises MID-PARSE, after some entities are already resolved
    real = graph_resolve.resolve_entity
    calls = {"n": 0}

    def exploding(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("synthetic mid-parse failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(graph_resolve, "resolve_entity", exploding)
    assert graph.index_document(conn, "bad-2", "HUB ALPHA BETA together.", "gws2") == 0
    monkeypatch.setattr(graph_resolve, "resolve_entity", real)

    assert conn.execute("SELECT COUNT(*) FROM graph_entities").fetchone()[0] == baseline_entities
    assert conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0] == baseline_edges
    assert not conn.execute(
        "SELECT 1 FROM graph_mentions WHERE doc_id IN ('bad-1','bad-2')").fetchone()

    # (c) the pipeline keeps going
    assert graph.index_document(conn, "ok-2", "ALPHA and BETA ship next.", "gws2") >= 2


def test_reingest_forgets_what_the_document_no_longer_says(conn):
    graph.index_document(conn, "r1", "ORION and VEGA and HUB.", "gws2")
    graph.index_document(conn, "r2", "ORION is still here.", "gws2")
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_entities WHERE label='VEGA'").fetchone()[0] == 1

    graph.index_document(conn, "r1", "nothing notable here anymore", "gws2")
    # VEGA was only ever in r1 -> gone, with its edges
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_entities WHERE label='VEGA'").fetchone()[0] == 0
    # ORION is still asserted by r2 -> survives
    assert conn.execute(
        "SELECT COUNT(*) FROM graph_entities WHERE label='ORION'").fetchone()[0] == 1
    dangling = conn.execute(
        "SELECT COUNT(*) FROM graph_edges g WHERE NOT EXISTS"
        " (SELECT 1 FROM graph_entities e WHERE e.id=g.src_id) OR NOT EXISTS"
        " (SELECT 1 FROM graph_entities e WHERE e.id=g.dst_id)").fetchone()[0]
    assert dangling == 0


def test_resolver_refuses_labels_it_cannot_key(conn):
    """The gates are structural, not confidence thresholds — a caller that skips
    extract_entities (the llm pass) hits the same wall."""
    with pytest.raises(ValueError):   # unit-less fragment under the money floor
        graph_resolve.resolve_entity(conn, "₹25", "money", "gws2",
                                     confidence=0.9, doc_id="d1")
    with pytest.raises(ValueError):   # not in the fixed vocabulary
        graph_resolve.resolve_entity(conn, "Wexford", "vendor", "gws2",
                                     confidence=0.9, doc_id="d1")
    with pytest.raises(ValueError):   # cite-or-silent
        graph_resolve.resolve_entity(conn, "Wexford", "org", "gws2",
                                     confidence=0.9, doc_id="")
    with pytest.raises(ValueError):   # nothing left after canonicalisation
        graph_resolve.resolve_entity(conn, "!!!", "org", "gws2",
                                     confidence=0.9, doc_id="d1")
    assert conn.execute("SELECT COUNT(*) FROM graph_entities").fetchone()[0] == 0


def test_panel_ranks_actors_above_scaffolding(conn):
    """PMI kills co-occurrence hubs but does not know a date is scaffolding.
    Measured on the live corpus: without the render prior the top 12 panel
    nodes were 7 dates. Same degree, same weight -> the actor wins."""
    product = graph_resolve.resolve_entity(conn, "Ravensbourne", "product", "gws2",
                                           confidence=0.9, doc_id="d1")
    date = graph_resolve.resolve_entity(conn, "2026-05-13", "date", "gws2",
                                        confidence=0.9, doc_id="d1")
    anchor = graph_resolve.resolve_entity(conn, "Wexford Partners", "org", "gws2",
                                          confidence=0.9, doc_id="d1")
    graph.add_edge(conn, product, anchor, "PART_OF", confidence=0.8, weight=5.0,
                   doc_id="d1", extractor="regex")
    graph.add_edge(conn, date, anchor, "DUE_ON", confidence=0.8, weight=5.0,
                   doc_id="d1", extractor="regex")

    ranked = {n["label"]: n["score"] for n in graph.top_entities(conn, limit=10)}
    assert ranked["Ravensbourne"] > ranked["2026-05-13"]
    assert "2026-05-13" in ranked, "scaffolding is demoted, never hidden"


def test_canonical_key_normalisation():
    assert graph_resolve.canonical_key("Acme's") == "acme"
    assert graph_resolve.canonical_key("  BLUE  leaf ") == "blue leaf"
    assert graph_resolve.canonical_key("Blue-leaf, Inc.") == "blue leaf inc"
    assert graph_resolve.canonical_key("Orion") == graph_resolve.canonical_key("ORION")
    assert graph_resolve.alias_key("Acme's") != graph_resolve.alias_key("Acme")


# ── 6b. isolation under a LOCKED database (verifier finding, 2026-07-27) ─────

def test_locked_db_returns_zero_instead_of_raising(conn, heydey_home):
    """The adversarial verifier found `BEGIN IMMEDIATE` sitting OUTSIDE the
    try block: a concurrent writer (a backfill stream) made index_document
    raise OperationalError instead of honouring the "one bad doc returns 0"
    contract. vector_store.upsert_points already retries this exact lock; the
    graph path now does too."""
    import sqlite3

    from heydey import workspaces as ws

    holder = sqlite3.connect(str(ws.db_path("gws2")), timeout=0.1)
    holder.execute("BEGIN IMMEDIATE")  # hold the write lock
    conn.execute("PRAGMA busy_timeout = 100")  # keep the suite fast (default is 30s)
    try:
        n = graph.index_document(conn, "locked-doc", "ORION ships. ORION is a product.", "gws2")
        assert n == 0  # degraded quietly, never raised
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    # and the very next document still indexes normally
    assert graph.index_document(conn, "after-doc", "ORION ships. ORION is a product.", "gws2") > 0
