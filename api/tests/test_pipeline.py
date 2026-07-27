"""Agent runtime + staged pipeline (S3) — fail policy, receipts, grounding.

Hermetic: embedding + both LLM lanes are stubbed. The live end-to-end proof is
`python -m heydey.s3_eval`. Here we pin the CONTROL FLOW that the gate depends on."""

import pytest

from heydey import ask, pipeline, vector_store, workspaces
from heydey.pipeline import AgentSpec, deterministic_grounding, run_pipeline

DIM = 384


def _axis(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


@pytest.fixture()
def conn(heydey_home, monkeypatch):
    workspaces.create_workspace("pipe")
    c = workspaces.connect("pipe")
    vector_store.upsert_points(c, [
        ("p1", _axis(0), {"doc_id": "d1", "text": "ORION pricing floor is 25 lakh, AMC separate.",
                          "source_file": "pricing.md", "chunk_index": 1, "created_at": "2026-06-23T10:00:00"}),
        ("p2", _axis(1), {"doc_id": "d2", "text": "Deploys use the Railway CLI only.",
                          "source_file": "deploy.md", "chunk_index": 0, "created_at": "2026-06-24T10:00:00"}),
    ])
    monkeypatch.setattr(ask, "embed_texts", lambda t: [_axis(0) for _ in t])
    yield c
    c.close()


def test_synthesized_pass_writes_receipts(conn):
    execu = lambda m, p, s, mt: "The ORION pricing floor is 25 lakh [1]."
    valid = lambda m, p, s, mt: '[{"i":0,"grounded":true,"chunk":1,"reason":"chunk 1"}]'
    r = run_pipeline(conn, AgentSpec("a", "Analyst"), "what is the pricing floor?",
                     executor_fn=execu, validator_fn=valid)
    assert r.answer_kind == "synthesized"
    assert r.validator_pass is True
    assert r.ungrounded_count == 0
    assert r.receipts and r.receipts[0]["validator_pass"] == 1
    assert r.receipts[0]["risk_tier"] == "read"  # answers are read-class (W2)
    # receipt persisted to the ledger
    n = conn.execute("SELECT COUNT(*) FROM receipts WHERE run_id=?", (r.run_id,)).fetchone()[0]
    assert n >= 1


def test_retry_then_degrade(conn):
    # executor always fabricates; validator always fails -> retry -> degrade extractive
    execu = lambda m, p, s, mt: "The moon is made of cheese and costs 5 crore [1]."
    valid = lambda m, p, s, mt: "[]"  # empty verdicts -> every sentence fails closed
    r = run_pipeline(conn, AgentSpec("a", "Analyst"), "what is the pricing floor?",
                     executor_fn=execu, validator_fn=valid)
    assert r.retry_used is True
    assert r.answer_kind == "validator-degraded"
    # the shipped text is a verbatim chunk (grounded by construction) — NOT the fabrication
    assert "cheese" not in r.answer
    assert "25 lakh" in r.answer
    assert r.ungrounded_count == 0  # gate holds: nothing ungrounded ships


def test_empty_store_is_a_state_not_a_crash(heydey_home, monkeypatch):
    workspaces.create_workspace("empty")
    c = workspaces.connect("empty")
    monkeypatch.setattr(ask, "embed_texts", lambda t: [_axis(0) for _ in t])
    r = run_pipeline(c, AgentSpec("a", "Analyst"), "anything",
                     executor_fn=lambda *a: "x", validator_fn=lambda *a: "[]")
    assert r.answer_kind == "empty"
    assert r.ungrounded_count == 0
    c.close()


def test_deterministic_grounding_catches_fabrication():
    hits = [{"payload": {"text": "The timeline is about two months of cash."}}]
    ok, _ = deterministic_grounding("The timeline is about two months of cash.", hits)
    assert ok is True
    bad, failed = deterministic_grounding("Blueleaf raised a 10 million dollar seed round.", hits)
    assert bad is False and failed


def test_hedge_is_grounded():
    hits = [{"payload": {"text": "unrelated content about deploys"}}]
    ok, _ = deterministic_grounding("The records don't cover this yet.", hits)
    assert ok is True  # a refusal makes no factual claim -> grounded


# ── S6c: AgentSpec.focus — retrieval-bias only, never a prompt input ─────────

def test_focus_biases_retrieval_query_only(conn, monkeypatch):
    """focus is appended to the retrieval query only. The synthesis prompt,
    validator input, receipts, and episodic intent all still receive the
    user's original question. (Return [] here so the pipeline exits early —
    the captured query IS the assertion.)"""
    captured: list[str] = []

    def fake_retrieve(_c, q, k=6):
        captured.append(q)
        return []

    monkeypatch.setattr(ask, "retrieve", fake_retrieve)
    spec = AgentSpec(id="a", name="X", focus="orders returns rate")
    run_pipeline(conn, spec, "what is the return rate?",
                 executor_fn=lambda *a: "", validator_fn=lambda *a: "[]")
    assert captured == ["what is the return rate? orders returns rate"]


def test_default_focus_leaves_retrieve_unchanged(conn, monkeypatch):
    """Empty focus reproduces the exact pre-S6c retrieve call — the guard that
    keeps all 154 existing tests green (no behavior change on the default)."""
    captured: list[str] = []

    def fake_retrieve(_c, q, k=6):
        captured.append(q)
        return []

    monkeypatch.setattr(ask, "retrieve", fake_retrieve)
    spec = AgentSpec(id="a", name="X")  # focus defaults to ""
    run_pipeline(conn, spec, "the plain question",
                 executor_fn=lambda *a: "", validator_fn=lambda *a: "[]")
    assert captured == ["the plain question"]


def test_s7_egress_blocks_cloud_when_connector_content_and_local_only(heydey_home, monkeypatch):
    """S7 · Contract C Layer 3: a connector-sourced hit + a local_only profile
    must refuse the cloud executor and degrade to verbatim extractive, labeled
    'egress-blocked'. The clinic/hospital DPDP posture depends on this."""
    from heydey import models_config, pipeline, vector_store, workspaces
    from heydey.pipeline import AgentSpec

    # define a local_only profile whose default executor is a CLOUD model
    p = models_config.Profile(
        name="dpdp-clinic",
        default=models_config.Pair("deepseek/deepseek-chat", "qwen3:8b"),
        egress="local_only",
    )
    models_config.save_profile(p)

    workspaces.create_workspace("s7ws")
    conn = workspaces.connect("s7ws")
    try:
        # a connector-sourced hit — the guard trigger
        vec = [0.0] * 384; vec[0] = 1.0
        vector_store.upsert_points(conn, [
            ("p-conn", vec, {
                "doc_id": "connector:demo-agency:list_intake",
                "text": "DEMO client: Northstar wants a brand refresh by Friday.",
                "source_file": "connector:demo-agency:list_intake",
                "chunk_index": 0,
                "created_at": "2026-07-21T00:00:00",
                "source_type": "connector",
                "connector_id": "demo-agency",
            }),
        ])
        monkeypatch.setattr("heydey.ask.embed_texts", lambda ts: [vec for _ in ts])

        # If the guard fires, no LLM call reaches the cloud — trap to prove it.
        def trap(*a, **k):
            raise AssertionError("cloud LLM called despite egress=local_only + connector content")
        monkeypatch.setattr("heydey.pipeline.llm_client.complete", trap)

        spec = AgentSpec(id="a", name="A", synthesize=True)
        result = pipeline.run_pipeline(conn, spec, "what's Northstar asking for?",
                                       profile="dpdp-clinic", workspace_id="s7ws")
        assert result.answer_kind == "egress-blocked"
        assert result.answer.startswith("DEMO client")  # verbatim extractive
    finally:
        conn.close()


def test_s7_egress_ok_when_no_connector_content(heydey_home, monkeypatch):
    """The egress guard fires ONLY on connector-sourced chunks — clean ops
    content still runs the cloud executor under a local_only profile (the
    guard is about regulated data, not about the model)."""
    from heydey import models_config, pipeline, vector_store, workspaces
    from heydey.pipeline import AgentSpec

    p = models_config.Profile(
        name="dpdp-clinic-2",
        default=models_config.Pair("deepseek/deepseek-chat", "qwen3:8b"),
        egress="local_only",
    )
    models_config.save_profile(p)

    workspaces.create_workspace("s7ws2")
    conn = workspaces.connect("s7ws2")
    try:
        vec = [0.0] * 384; vec[0] = 1.0
        vector_store.upsert_points(conn, [
            ("p-ops", vec, {
                "doc_id": "d-ops", "text": "Ops corpus chunk, no connector origin.",
                "source_file": "notes.md", "chunk_index": 0,
                "created_at": "2026-07-21T00:00:00",
                # no source_type: 'connector'
            }),
        ])
        monkeypatch.setattr("heydey.ask.embed_texts", lambda ts: [vec for _ in ts])

        called = []
        def fake_synth(model, prompt, system, max_tokens):
            called.append(model)
            return "The ops corpus chunk covers the question."
        def fake_valid(model, prompt, system, max_tokens):
            return '[{"i": 0, "grounded": true, "chunk": 1, "confidence": 0.9}]'
        # bypass real LLM but keep the code path live
        spec = AgentSpec(id="b", name="B", synthesize=True)
        result = pipeline.run_pipeline(conn, spec, "what does ops carry?",
                                       profile="dpdp-clinic-2", workspace_id="s7ws2",
                                       executor_fn=fake_synth, validator_fn=fake_valid)
        assert result.answer_kind != "egress-blocked"
        assert called == ["deepseek/deepseek-chat"]
    finally:
        conn.close()
