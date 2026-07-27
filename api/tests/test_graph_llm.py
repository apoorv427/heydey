"""G2 — the LLM triple extractor + the resumable backfill runner.

Two things are under test and both are failure-shaped:

1. :mod:`heydey.graph_llm` is a FILTER. The diagnosis named hallucination as the
   likeliest way the graph rebuild goes wrong, so the tests here are mostly proofs
   that bad triples are DROPPED — ungrounded endpoints, unknown predicates, unknown
   types, self-loops, garbage JSON, a dead model.
2. :mod:`heydey.graph_backfill` is a MULTI-HOUR run. It is only useful if a kill at
   hour 3 resumes at document N+1 and one poisoned document cannot abort the rest.

Every test injects `complete_fn` (and, for the runner, `persist_fn`): no network, no
Ollama, no sleeping, and no dependency on G1's writers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heydey import graph_backfill, graph_llm, workspaces

# ── helpers ──────────────────────────────────────────────────────────────────

TEXT = "Dana Whitfield decided that ORION depends on Heydey. ORION ships in August."


def reply(*triples: dict):
    """A fake complete_fn returning a fixed model reply (dict/list -> JSON, str -> raw)."""
    payload = {"triples": list(triples)}

    def complete_fn(model, prompt, **kwargs):
        return json.dumps(payload)

    return complete_fn


def triple(subject="ORION", subject_type="product", predicate="DEPENDS_ON",
           obj="Heydey", object_type="product") -> dict:
    return {"subject": subject, "subject_type": subject_type, "predicate": predicate,
            "object": obj, "object_type": object_type}


def raw_reply(text: str):
    def complete_fn(model, prompt, **kwargs):
        return text

    return complete_fn


@pytest.fixture()
def conn(heydey_home):
    workspaces.create_workspace("g2ws")
    c = workspaces.connect("g2ws")
    yield c
    c.close()


def add_doc(conn, doc_id: str, chunks: list[str]) -> None:
    """Put a document into `points` — the only corpus the backfill ever reads."""
    for index, text in enumerate(chunks):
        conn.execute(
            "INSERT INTO points(point_id, doc_id, source_file, created_at, text, payload)"
            " VALUES (?,?,?,?,?,?)",
            (f"pt-{doc_id}-{index}", doc_id, f"{doc_id}.md", "2026-07-27T00:00:00", text,
             json.dumps({"doc_id": doc_id, "chunk_id": f"{doc_id}#c{index}", "text": text})),
        )
    conn.commit()


class Persist:
    """Stand-in for graph_llm.persist_triples (G1 owns the real writers). Records what
    the runner asked to persist and can be told to blow up on a given document."""

    def __init__(self, fail_on: str | None = None, exc=RuntimeError("poisoned doc")):
        self.calls: list[tuple[str, str, list[dict]]] = []
        self.fail_on, self.exc = fail_on, exc

    def __call__(self, conn, triples, *, workspace_id, doc_id, chunk_id=None):
        if doc_id == self.fail_on:
            raise self.exc
        self.calls.append((doc_id, chunk_id, list(triples)))
        return len({t["subject"] for t in triples} | {t["object"] for t in triples}), len(triples)

    @property
    def doc_ids(self) -> list[str]:
        return [doc for doc, _, _ in self.calls]


def quiet(*_args, **_kwargs) -> None:
    """progress sink — keeps the suite output clean."""


def ledger(conn) -> dict[str, tuple[str, str]]:
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT doc_id, pass, status FROM graph_backfill")}


# ── graph_llm: the happy path + the confidence rule ───────────────────────────
def test_valid_triple_survives_with_full_shape():
    out = graph_llm.extract_triples(TEXT, complete_fn=reply(triple()))
    assert out == [{
        "subject": "ORION", "subject_type": "product", "predicate": "DEPENDS_ON",
        "object": "Heydey", "object_type": "product", "confidence": 0.6,
    }]


def test_confidence_is_08_only_when_both_endpoints_recur():
    """0.8 needs BOTH endpoints to recur — a triple is only as grounded as its weaker
    endpoint. Here 'Heydey' appears once in TEXT (0.6) but twice in the document."""
    once = graph_llm.extract_triples(TEXT, complete_fn=reply(triple()))
    assert once[0]["confidence"] == 0.6

    doc = TEXT + " Heydey is the operating system."
    recurring = graph_llm.extract_triples(TEXT, complete_fn=reply(triple()), doc_text=doc)
    assert recurring[0]["confidence"] == 0.8


# ── graph_llm: THE guard — hallucinated endpoints are dropped ─────────────────
def test_hallucinated_subject_is_dropped():
    """'Acme Corp' is nowhere in the source text — the model invented it. The grounded
    triple in the same reply must still survive (drop the triple, not the batch)."""
    out = graph_llm.extract_triples(TEXT, complete_fn=reply(
        triple(subject="Acme Corp", subject_type="org"),
        triple(),
    ))
    assert [t["subject"] for t in out] == ["ORION"]


def test_hallucinated_object_is_dropped():
    out = graph_llm.extract_triples(TEXT, complete_fn=reply(
        triple(obj="Project Nimbus")))
    assert out == []


def test_grounding_tolerates_whitespace_and_case_only():
    """The check normalises whitespace + case (a model re-wraps lines), and nothing
    else — a renamed or expanded entity is still a hallucination."""
    text = "Dana   Whitfield\nowns ORION."
    owns = dict(subject_type="person", predicate="OWNS", obj="ORION", object_type="product")
    assert graph_llm.extract_triples(text, complete_fn=reply(
        triple(subject="dana whitfield", **owns)))
    assert graph_llm.extract_triples(text, complete_fn=reply(
        triple(subject="Dana Whitfield Jr", **owns))) == []


# ── graph_llm: whitelist, never coerce ────────────────────────────────────────
def test_unknown_predicate_is_dropped_not_coerced():
    """'RELATES_TO' is not in the vocabulary. It must vanish — never be rewritten as
    MENTIONS/DEPENDS_ON, because a wrong typed edge reads as a fact."""
    out = graph_llm.extract_triples(TEXT, complete_fn=reply(
        triple(predicate="RELATES_TO"),
        triple(subject="Dana Whitfield", subject_type="person",
               predicate="DECIDED_BY", obj="ORION", object_type="product"),
    ))
    assert [t["predicate"] for t in out] == ["DECIDED_BY"]
    assert all(t["predicate"] in graph_llm.vocabulary()[1] for t in out)


def test_unknown_entity_type_is_dropped():
    out = graph_llm.extract_triples(TEXT, complete_fn=reply(
        triple(subject_type="project")))  # 'project' is not in ENTITY_TYPES
    assert out == []


def test_case_canonicalisation_is_the_only_normalisation():
    """Predicates are uppercase and types lowercase by construction, so case folding
    cannot map one vocabulary term onto another — it is safe, and it is the ONLY
    rewriting allowed."""
    out = graph_llm.extract_triples(TEXT, complete_fn=reply(
        triple(predicate="depends_on", subject_type="Product", object_type="PRODUCT")))
    assert out and out[0]["predicate"] == "DEPENDS_ON"
    assert out[0]["subject_type"] == "product" and out[0]["object_type"] == "product"


def test_self_loops_and_duplicates_are_dropped():
    out = graph_llm.extract_triples(TEXT, complete_fn=reply(
        triple(obj="ORION"),   # self-loop
        triple(), triple(),      # exact duplicate
    ))
    assert len(out) == 1 and out[0]["object"] == "Heydey"


# ── graph_llm: fail closed and quiet ──────────────────────────────────────────
@pytest.mark.parametrize("garbage", [
    "I think ORION depends on Heydey, honestly.",   # prose, no JSON
    "{\"triples\": [{\"subject\": \"ORION\",",       # truncated JSON
    "",                                                # empty completion
    "[1, 2, 3]",                                       # right shape, wrong contents
    "{\"triples\": \"ORION depends on Heydey\"}",    # wrong value type
])
def test_malformed_model_output_returns_empty_and_never_raises(garbage):
    assert graph_llm.extract_triples(TEXT, complete_fn=raw_reply(garbage)) == []


def test_model_failure_returns_empty_and_never_raises():
    """A dead Ollama must not raise into an ingest/backfill loop."""
    def boom(model, prompt, **kwargs):
        raise RuntimeError("ollama unreachable (qwen3:8b)")

    assert graph_llm.extract_triples(TEXT, complete_fn=boom) == []


def test_empty_text_makes_no_model_call():
    def never(model, prompt, **kwargs):
        raise AssertionError("model called on empty text")

    assert graph_llm.extract_triples("   ", complete_fn=never) == []


def test_completion_object_and_bare_string_both_accepted():
    """complete_fn may return a Completion (llm_client) or a plain string."""
    class Completion:
        text = json.dumps([triple()])  # bare list shape, no {"triples": ...} wrapper

    assert graph_llm.extract_triples(TEXT, complete_fn=lambda *a, **k: Completion())


def test_graph_py_is_the_vocabulary_source_of_truth(monkeypatch):
    """The local copy is a fallback for import-order only. Once graph.py exports the
    vocabulary it wins, so the whitelist can never drift into two truths."""
    from heydey import graph

    monkeypatch.setattr(graph, "ENTITY_TYPES", frozenset({"product"}), raising=False)
    monkeypatch.setattr(graph, "PREDICATES", frozenset({"BLOCKS"}), raising=False)
    assert graph_llm.vocabulary() == (frozenset({"product"}), frozenset({"BLOCKS"}))
    assert graph_llm.extract_triples(TEXT, complete_fn=reply(triple())) == []  # DEPENDS_ON now unknown


# ── graph_llm: the hand-off to G1's writers ──────────────────────────────────
def test_persist_triples_hands_off_provenanced_edges(conn, monkeypatch):
    """Pins THIS module's half of the G1 contract: both endpoints resolved, exactly one
    typed edge per triple, and doc_id + chunk_id + extractor='llm' on every edge —
    cite-or-silent for the graph. The writers are stubbed with the guaranteed
    signatures, so a drift on this side shows up as a TypeError right here."""
    import sys
    import types

    from heydey import graph

    resolved, written = [], []

    def resolve_entity(c, label, etype, workspace_id, *, confidence, doc_id, chunk_id=None):
        resolved.append((label, etype, workspace_id, confidence, doc_id, chunk_id))
        return {"ORION": 1, "Heydey": 2}[label]

    def add_edge(c, src_id, dst_id, predicate, *, confidence, weight=1.0, doc_id,
                 chunk_id=None, extractor):
        written.append((src_id, dst_id, predicate, confidence, doc_id, chunk_id, extractor))
        return True

    stub = types.ModuleType("heydey.graph_resolve")
    stub.resolve_entity = resolve_entity
    monkeypatch.setitem(sys.modules, "heydey.graph_resolve", stub)
    monkeypatch.setattr(graph, "add_edge", add_edge, raising=False)

    triples = graph_llm.extract_triples(TEXT, complete_fn=reply(triple()))
    counts = graph_llm.persist_triples(conn, triples, workspace_id="g2ws",
                                       doc_id="doc-1", chunk_id="doc-1#c0")
    assert counts == (2, 1)
    assert written == [(1, 2, "DEPENDS_ON", 0.6, "doc-1", "doc-1#c0", "llm")]
    assert [row[0] for row in resolved] == ["ORION", "Heydey"]
    assert all(row[4:] == ("doc-1", "doc-1#c0") for row in resolved)


def test_a_label_the_resolver_refuses_drops_one_triple_not_the_document(conn, monkeypatch):
    """resolve_entity raises ValueError on a label it will not key (money under the
    floor, empty canonical key). That is a verdict on one triple — the rest of the
    document must still land."""
    import sys
    import types

    from heydey import graph

    def resolve_entity(c, label, etype, workspace_id, *, confidence, doc_id, chunk_id=None):
        if label == "Heydey":
            raise ValueError("label 'Heydey' has no canonical key")
        return abs(hash(label)) % 1000

    stub = types.ModuleType("heydey.graph_resolve")
    stub.resolve_entity = resolve_entity
    monkeypatch.setitem(sys.modules, "heydey.graph_resolve", stub)
    monkeypatch.setattr(graph, "add_edge", lambda *a, **k: True, raising=False)

    triples = graph_llm.extract_triples(TEXT, complete_fn=reply(
        triple(),  # object 'Heydey' -> refused
        triple(subject="Dana Whitfield", subject_type="person",
               predicate="DECIDED_BY", obj="ORION", object_type="product"),
    ))
    assert len(triples) == 2
    assert graph_llm.persist_triples(conn, triples, workspace_id="g2ws",
                                     doc_id="doc-1", chunk_id="c0") == (2, 1)


# ── backfill: resumability ────────────────────────────────────────────────────
def test_backfill_resumes_exactly_where_a_kill_stopped(conn):
    """Three docs; the model call dies on doc-3 the way Ctrl-C dies (BaseException,
    so the per-doc handler does NOT swallow it). Re-running must process doc-3 only."""
    for name in ("doc-1", "doc-2", "doc-3"):
        add_doc(conn, name, [f"{TEXT} Marker {name}."])

    def kill_on_third(model, prompt, **kwargs):
        if "doc-3" in prompt:
            raise KeyboardInterrupt("simulated kill")
        return json.dumps({"triples": [triple()]})

    first = Persist()
    with pytest.raises(KeyboardInterrupt):
        graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                                complete_fn=kill_on_third, persist_fn=first, progress=quiet)
    assert first.doc_ids == ["doc-1", "doc-2"]
    assert ledger(conn) == {"doc-1": ("llm", "done"), "doc-2": ("llm", "done")}

    second = Persist()
    report = graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                                     complete_fn=reply(triple()), persist_fn=second,
                                     progress=quiet)
    assert second.doc_ids == ["doc-3"], "a resumed run must not redo settled documents"
    assert report["processed"] == 1 and report["already_done"] == 2
    assert report["docs_total"] == 3 and report["done"] == 1
    assert set(ledger(conn)) == {"doc-1", "doc-2", "doc-3"}


def test_resumption_is_per_pass(conn):
    """A document settled by the deterministic pass is NOT skipped by the llm pass."""
    add_doc(conn, "doc-1", [TEXT])
    graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="deterministic",
                            progress=quiet)
    persist = Persist()
    report = graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                                     complete_fn=reply(triple()), persist_fn=persist,
                                     progress=quiet)
    assert report["processed"] == 1 and persist.doc_ids == ["doc-1"]


# ── backfill: per-document isolation ──────────────────────────────────────────
def test_poisoned_doc_is_recorded_failed_and_the_run_continues(conn):
    for name in ("doc-1", "doc-2", "doc-3"):
        add_doc(conn, name, [f"{TEXT} Marker {name}."])

    persist = Persist(fail_on="doc-2")
    report = graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                                     complete_fn=reply(triple()), persist_fn=persist,
                                     progress=quiet)
    assert report["processed"] == 3 and report["done"] == 2 and report["failed"] == 1
    assert persist.doc_ids == ["doc-1", "doc-3"], "docs after the bad one still ran"

    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT doc_id, status, error FROM graph_backfill")}
    assert rows["doc-2"][0] == "failed" and "poisoned doc" in rows["doc-2"][1]
    assert rows["doc-1"] == ("done", None) and rows["doc-3"] == ("done", None)

    # failed is NOT terminal — the next run retries it, and only it
    retry = Persist()
    again = graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                                    complete_fn=reply(triple()), persist_fn=retry,
                                    progress=quiet)
    assert retry.doc_ids == ["doc-2"] and again["done"] == 1


def test_per_doc_deadline_fails_that_doc_only(conn):
    """An already-expired deadline is the no-sleep proof that the hard per-doc timeout
    is enforced between chunks."""
    add_doc(conn, "doc-slow", [TEXT])
    add_doc(conn, "doc-ok", [TEXT])
    report = graph_backfill.backfill(
        conn, workspace_id="g2ws", pass_name="llm", doc_ids=["doc-slow"],
        doc_timeout=-1.0, complete_fn=reply(triple()), persist_fn=Persist(), progress=quiet)
    assert report["failed"] == 1
    row = conn.execute("SELECT status, error FROM graph_backfill WHERE doc_id='doc-slow'").fetchone()
    assert row[0] == "failed" and "deadline" in row[1]

    ok = graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                                 complete_fn=reply(triple()), persist_fn=Persist(),
                                 progress=quiet)
    assert ok["done"] == 2, "the timed-out doc is retried and the healthy one is unaffected"


def test_document_with_no_text_is_skipped_not_failed(conn):
    conn.execute("INSERT INTO points(point_id, doc_id, text, payload) VALUES (?,?,?,?)",
                 ("pt-empty", "doc-empty", "", "{}"))
    conn.commit()
    report = graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                                     complete_fn=reply(triple()), persist_fn=Persist(),
                                     progress=quiet)
    assert report["skipped"] == 1 and report["failed"] == 0


# ── backfill: what it reads, and what it selects ──────────────────────────────
def test_representative_chunks_are_the_dense_ones(conn):
    """Selection is entity density first, length as the tie-break — the longest chunk
    in this corpus is usually a log dump."""
    chunks = [
        {"chunk_id": "c0", "text": "table of contents " * 40},                 # long, empty
        {"chunk_id": "c1", "text": "Dana Whitfield and ORION and Heydey " * 5},
        {"chunk_id": "c2", "text": "short"},                                    # under the floor
        {"chunk_id": "c3", "text": "Acme owns ORION and Heydey and ORION and VEGA. " * 4},
    ]
    picked = [c["chunk_id"] for c in graph_backfill.select_chunks(chunks, 2)]
    assert picked == ["c1", "c3"], "densest two, returned in document order"
    assert graph_backfill.select_chunks(chunks, 0) == []
    assert len(graph_backfill.select_chunks(chunks, 99)) == 4


def test_backfill_reads_points_and_never_re_embeds(conn):
    """Lock L7: the migrated KB is copied in place. The runner must read stored text
    and never touch the embedder."""
    source = (Path(__file__).resolve().parents[1] / "heydey" / "graph_backfill.py").read_text()
    assert "embedder" not in source and "embed_texts" not in source
    add_doc(conn, "doc-1", ["Dana Whitfield owns ORION."])
    seen = {}

    def complete_fn(model, prompt, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"triples": []})

    graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                            complete_fn=complete_fn, persist_fn=Persist(), progress=quiet)
    assert "Dana Whitfield owns ORION." in seen["prompt"]


def test_llm_pass_carries_chunk_provenance(conn):
    """cite-or-silent for the graph: the writer is handed the chunk that asserted it."""
    add_doc(conn, "doc-1", [TEXT])
    persist = Persist()
    graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                            complete_fn=reply(triple()), persist_fn=persist, progress=quiet)
    assert persist.calls[0][0] == "doc-1" and persist.calls[0][1] == "doc-1#c0"


def test_limit_stops_early_and_the_rest_stay_todo(conn):
    for name in ("doc-1", "doc-2", "doc-3"):
        add_doc(conn, name, [TEXT])
    report = graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm", limit=2,
                                     complete_fn=reply(triple()), persist_fn=Persist(),
                                     progress=quiet)
    assert report["processed"] == 2 and len(ledger(conn)) == 2


def test_concurrent_streams_process_every_doc_on_one_connection(conn):
    """--concurrency runs N model streams; DB access must stay on the calling thread
    (a cross-thread sqlite3 connection raises ProgrammingError, so this test fails
    loudly if any DB call leaks into a worker)."""
    names = [f"doc-{i}" for i in range(6)]
    for name in names:
        add_doc(conn, name, [f"{TEXT} Marker {name}."])
    persist = Persist()
    report = graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="llm",
                                     concurrency=3, complete_fn=reply(triple()),
                                     persist_fn=persist, progress=quiet)
    assert report["processed"] == 6 and report["done"] == 6
    assert sorted(persist.doc_ids) == names
    assert set(ledger(conn)) == set(names)


def test_unknown_pass_name_raises(conn):
    with pytest.raises(ValueError):
        graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="magic", progress=quiet)


# ── backfill: the deterministic pass (no model, no network) ───────────────────
def test_deterministic_pass_runs_without_a_model(conn, monkeypatch):
    from heydey import graph

    monkeypatch.setattr(graph, "KNOWN_PROJECTS", ["ORION", "VEGA", "Heydey"])
    add_doc(conn, "doc-1", ["ORION is LOCKED under L28.", "VEGA is a GATE for S3."])
    report = graph_backfill.backfill(conn, workspace_id="g2ws", pass_name="deterministic",
                                     progress=quiet)
    assert report["done"] == 1 and report["failed"] == 0 and report["entities"] > 0
    assert ledger(conn) == {"doc-1": ("deterministic", "done")}
