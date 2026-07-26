"""S6c: HTTP surface — /foundry/onboard, /foundry/status, /ask?agent=.

Hermetic where practical: the embedder is stubbed to a fixed unit vector, so
retrieval + upsert stay offline; real ``connector_sync.sync`` spawns the demo
MCP subprocess for the happy-path setup (same shape as test_foundry.py) so
the route is proven end to end. Full-mode /ask uses a monkeypatched
run_pipeline (like test_server_s4a.py) — the pipeline contract itself is
tested in test_pipeline.py; here we only prove the route wires body.agent
through it and the first_answer event fires exactly once.
"""

import dataclasses
import json

import pytest

from heydey import ask, connector_sync, foundry, pipeline, server, vector_store, workspaces
from heydey.connector_sync import KNOWN_SERVERS

from conftest import auth_headers

DIM = 384


def _axis(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


@pytest.fixture(autouse=True)
def _fast_embed(monkeypatch):
    """No fastembed model load — both bindings must be replaced (each module
    imported ``embed_texts`` into its own namespace)."""
    monkeypatch.setattr(vector_store, "embed_texts", lambda t: [_axis(0) for _ in t])
    monkeypatch.setattr(ask, "embed_texts", lambda t: [_axis(0) for _ in t])


@pytest.fixture()
def ws(client, heydey_home):
    """A fresh workspace + a live client. Return the workspace id."""
    workspaces.create_workspace("s6cws")
    return "s6cws"


def _valid_answers(**overrides) -> dict:
    ans = {
        "business_type": "d2c",
        "company_name": "DEMO Northstar",
        "primary_goal": "cited_answers",
        "sources": ["demo-shopify"],
        "answer_style": "verbatim",
    }
    ans.update(overrides)
    return ans


def _sync_source(workspace_id: str, connector_id: str) -> dict:
    """Real MCP sync — same shape test_foundry.py + test_connector_sync.py use."""
    conn = workspaces.connect(workspace_id)
    try:
        return connector_sync.sync(conn, workspace_id, connector_id,
                                   KNOWN_SERVERS[connector_id])
    finally:
        conn.close()


# ── /foundry/onboard ─────────────────────────────────────────────────────────

def test_onboard_requires_token(client, ws):
    """POST is bearer-gated by the mutation middleware — no token, no work."""
    resp = client.post("/foundry/onboard",
                       json={"workspace": ws, "answers": _valid_answers()})
    assert resp.status_code == 401


def test_onboard_unknown_workspace_404(client, heydey_home):
    resp = client.post("/foundry/onboard",
                       json={"workspace": "no-such-ws",
                             "answers": _valid_answers()},
                       headers=auth_headers())
    assert resp.status_code == 404


def test_onboard_bad_answers_422(client, ws):
    """FoundryError -> 422 with the reason in ``detail``. agent_specs stays empty."""
    resp = client.post("/foundry/onboard",
                       json={"workspace": ws,
                             "answers": _valid_answers(business_type="hospital")},
                       headers=auth_headers())
    assert resp.status_code == 422
    assert "hospital" in resp.json()["detail"]

    # all-or-nothing: no rows leaked from the failed run
    conn = workspaces.connect(ws)
    try:
        n = conn.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_onboard_injection_shape_company_name_422(client, ws):
    resp = client.post("/foundry/onboard",
                       json={"workspace": ws,
                             "answers": _valid_answers(company_name="Acme{ignore previous")},
                       headers=auth_headers())
    assert resp.status_code == 422


def test_onboard_unsynced_source_422(client, ws):
    """The UI's error-with-next-step: connect+sync first, then onboard."""
    resp = client.post("/foundry/onboard",
                       json={"workspace": ws, "answers": _valid_answers()},
                       headers=auth_headers())
    assert resp.status_code == 422
    assert "sync a source first" in resp.json()["detail"]


def test_onboard_happy_path_returns_specs_and_scan(client, ws):
    """Real MCP sync, real instantiate, real return shape."""
    _sync_source(ws, "demo-shopify")

    resp = client.post("/foundry/onboard",
                       json={"workspace": ws, "answers": _valid_answers()},
                       headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()

    # exact shape per contract §C: {"playbook", "specs", "scan", "elapsed_ms"}
    assert set(body) == {"playbook", "specs", "scan", "elapsed_ms"}
    assert body["playbook"] == "d2c-ops"
    assert 3 <= len(body["specs"]) <= 6      # N in [3, 6] by construction
    for spec in body["specs"]:
        assert spec["validator_pass"] == 1
        assert spec["playbook"] == "d2c-ops"
    assert body["scan"]["clean_sources"] == ["demo-shopify"]
    assert body["scan"]["chunks"] > 0
    assert isinstance(body["elapsed_ms"], (int, float))
    assert body["elapsed_ms"] >= 0


# ── /foundry/status ──────────────────────────────────────────────────────────

def test_status_unknown_workspace_404(client, heydey_home):
    resp = client.get("/foundry/status?workspace=no-such-ws")
    assert resp.status_code == 404


def test_status_empty_workspace_shape(client, ws):
    resp = client.get(f"/foundry/status?workspace={ws}")
    assert resp.status_code == 200
    body = resp.json()
    # Full shape — the webapp reads INTERVIEW from here (single source of truth)
    assert set(body) == {"workspace", "phase", "scan", "specs", "events", "interview"}
    assert body["workspace"] == ws
    assert body["phase"] == "empty"
    assert body["specs"] == []
    assert body["events"] == []
    # INTERVIEW ships as the exact module constant — 5 questions, no hardcoded text on the UI
    assert body["interview"] == foundry.INTERVIEW
    assert len(body["interview"]) == 5
    assert {q["key"] for q in body["interview"]} == {
        "business_type", "company_name", "primary_goal", "sources", "answer_style"
    }


def test_status_phase_tracks_state(client, ws):
    # empty -> sources_ready -> fleet_live
    empty = client.get(f"/foundry/status?workspace={ws}").json()
    assert empty["phase"] == "empty"

    _sync_source(ws, "demo-shopify")
    ready = client.get(f"/foundry/status?workspace={ws}").json()
    assert ready["phase"] == "sources_ready"
    assert ready["scan"]["clean_sources"] == ["demo-shopify"]

    onboard = client.post("/foundry/onboard",
                          json={"workspace": ws, "answers": _valid_answers()},
                          headers=auth_headers())
    assert onboard.status_code == 200
    live = client.get(f"/foundry/status?workspace={ws}").json()
    assert live["phase"] == "fleet_live"
    assert len(live["specs"]) >= 3
    # DESC-ordered event log -> the freshest step is first
    assert live["events"][0]["step"] == "fleet_instantiated"


# ── /ask agent param ─────────────────────────────────────────────────────────

def test_ask_agent_unknown_returns_404(client, ws):
    resp = client.post("/ask",
                       json={"question": "q", "workspace": ws, "agent": "nope"},
                       headers=auth_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown or unvalidated agent"


def test_ask_agent_unvalidated_row_returns_404(client, ws):
    """A hand-inserted validator_pass=0 row is invisible to /ask — fail-closed."""
    conn = workspaces.connect(ws)
    try:
        conn.execute(
            "INSERT INTO agent_specs(id, name, version, spec_json, "
            "                        validator_pass, created_at) "
            "VALUES ('unvalidated', 'X', 1, ?, 0, '2026-07-21T00:00:00Z')",
            (json.dumps({"id": "unvalidated", "name": "X", "task_class": "ask",
                         "k": 6, "synthesize": False, "role": "r", "focus": "",
                         "playbook": "d2c-ops"}),),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.post("/ask",
                       json={"question": "q", "workspace": ws, "agent": "unvalidated"},
                       headers=auth_headers())
    assert resp.status_code == 404


def test_ask_agent_evidence_uses_focus_and_spec_k(client, ws, monkeypatch):
    """Agent-mode evidence must route through spec.focus + spec.k — never body.k.

    Captures the exact retrieval args ask.retrieve receives, then asserts the
    focus is appended and the k came from the spec (not the request body)."""
    _sync_source(ws, "demo-shopify")
    # Onboard so a validated d2c-librarian row exists (k=8, focus="", synthesize=False)
    onboard = client.post("/foundry/onboard",
                          json={"workspace": ws, "answers": _valid_answers()},
                          headers=auth_headers())
    assert onboard.status_code == 200

    captured: list[tuple] = []
    real_retrieve = ask.retrieve

    def fake_retrieve(conn, q, k=6):
        captured.append((q, k))
        return real_retrieve(conn, q, k=k)

    monkeypatch.setattr(server.ask, "retrieve", fake_retrieve)

    # d2c-analyst has k=8 (cited_answers bump), focus="orders returns revenue sku"
    resp = client.post(
        "/ask",
        json={"question": "how many orders were rto?", "workspace": ws,
              "agent": "d2c-analyst", "k": 3},  # body.k=3 must be IGNORED
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "evidence"

    # The captured call must show focus appended, spec.k (=8) used — body.k ignored
    assert captured, "ask.retrieve was not called"
    q, k = captured[0]
    assert q == "how many orders were rto? orders returns revenue sku"
    assert k == 8


def test_ask_agent_evidence_empty_focus_uses_bare_question(client, ws, monkeypatch):
    """Librarian has focus="" — retrieval must receive the plain question, not
    a trailing space or the focus placeholder."""
    _sync_source(ws, "demo-shopify")
    client.post("/foundry/onboard",
                json={"workspace": ws, "answers": _valid_answers()},
                headers=auth_headers())

    captured: list[tuple] = []
    real_retrieve = ask.retrieve

    def fake_retrieve(conn, q, k=6):
        captured.append((q, k))
        return real_retrieve(conn, q, k=k)

    monkeypatch.setattr(server.ask, "retrieve", fake_retrieve)

    resp = client.post(
        "/ask",
        json={"question": "cite this", "workspace": ws, "agent": "d2c-librarian"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    q, k = captured[0]
    assert q == "cite this"     # no trailing space, no focus appended
    assert k == 8               # librarian is always k=8


def test_ask_agent_full_mode_hydrates_spec_and_ignores_body_k(client, ws, monkeypatch):
    """Full mode routes through run_pipeline with the hydrated spec — body.k is
    IGNORED (§C: the spec IS the config)."""
    _sync_source(ws, "demo-shopify")
    client.post("/foundry/onboard",
                json={"workspace": ws, "answers": _valid_answers()},
                headers=auth_headers())

    seen_spec = {}
    fake = pipeline.RunResult(
        run_id="run-x", question="q", answer="cited",
        answer_kind="extractive", validator_status="validated",
        validator_pass=True, executor_model="llama3.1:8b",
        validator_model="qwen3:8b", badge="ok",
        citations=[{"source": "demo-shopify"}], receipts=[],
        ungrounded_count=0, cost_usd=0.0, duration_s=0.1,
    )

    def fake_run(_conn, spec, _q, **_kw):
        seen_spec["spec"] = spec
        return fake

    monkeypatch.setattr(server.pipeline, "run_pipeline", fake_run)

    resp = client.post(
        "/ask",
        json={"question": "q", "workspace": ws, "mode": "full",
              "agent": "d2c-librarian", "k": 2},  # body.k=2 ignored
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    spec = seen_spec["spec"]
    # The hydrated spec (from foundry.get_spec) — not an ad-hoc ask-ui spec
    assert isinstance(spec, pipeline.AgentSpec)
    assert spec.id == "d2c-librarian"
    assert spec.k == 8                # from the shelf, not from body.k=2
    assert spec.synthesize is False
    assert spec.playbook == "d2c-ops"


def test_ask_without_agent_still_uses_body_k(client, ws, monkeypatch):
    """Regression guard: the additive agent param must not change the pre-S6c
    default path. body.k still flows through when agent is unset."""
    captured: list[tuple] = []

    def fake_retrieve(_conn, q, k=6):
        captured.append((q, k))
        return []

    monkeypatch.setattr(server.ask, "retrieve", fake_retrieve)

    resp = client.post("/ask",
                       json={"question": "plain", "workspace": ws, "k": 4},
                       headers=auth_headers())
    assert resp.status_code == 200
    assert captured[0] == ("plain", 4)


# ── first_answer stopwatch tick (SELECT-1 guard) ─────────────────────────────

def test_first_answer_written_exactly_once(client, ws):
    """Every subsequent agent-run in the same workspace must NOT insert another
    first_answer row — the M:SS stopwatch fires once, then never again."""
    _sync_source(ws, "demo-shopify")
    client.post("/foundry/onboard",
                json={"workspace": ws, "answers": _valid_answers()},
                headers=auth_headers())

    def count_first_answers() -> int:
        conn = workspaces.connect(ws)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM foundry_events "
                "WHERE workspace_id = ? AND step = 'first_answer'",
                (ws,),
            ).fetchone()[0]
        finally:
            conn.close()

    assert count_first_answers() == 0  # onboarding alone doesn't fire it

    # first agent-answered request
    r1 = client.post("/ask",
                     json={"question": "cite this", "workspace": ws,
                           "agent": "d2c-librarian"},
                     headers=auth_headers())
    assert r1.status_code == 200
    assert count_first_answers() == 1

    # a second request must NOT insert another row (SELECT-1 idempotency)
    r2 = client.post("/ask",
                     json={"question": "another question", "workspace": ws,
                           "agent": "d2c-librarian"},
                     headers=auth_headers())
    assert r2.status_code == 200
    assert count_first_answers() == 1

    # a third request with a DIFFERENT agent is still one row (guard is per-workspace)
    r3 = client.post("/ask",
                     json={"question": "third", "workspace": ws,
                           "agent": "d2c-analyst"},
                     headers=auth_headers())
    assert r3.status_code == 200
    assert count_first_answers() == 1


def test_first_answer_does_not_fire_without_agent(client, ws):
    """Ad-hoc /ask (no agent) must NEVER write a first_answer row — it's the
    Architect's tick, not the user's."""
    _sync_source(ws, "demo-shopify")
    client.post("/foundry/onboard",
                json={"workspace": ws, "answers": _valid_answers()},
                headers=auth_headers())

    resp = client.post("/ask",
                       json={"question": "cite this", "workspace": ws},
                       headers=auth_headers())
    assert resp.status_code == 200

    conn = workspaces.connect(ws)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM foundry_events "
            "WHERE workspace_id = ? AND step = 'first_answer'",
            (ws,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_first_answer_detail_carries_agent_and_citations(client, ws):
    _sync_source(ws, "demo-shopify")
    client.post("/foundry/onboard",
                json={"workspace": ws, "answers": _valid_answers()},
                headers=auth_headers())

    resp = client.post("/ask",
                       json={"question": "cite this", "workspace": ws,
                             "agent": "d2c-librarian"},
                       headers=auth_headers())
    assert resp.status_code == 200
    n_citations = len(resp.json()["citations"])

    conn = workspaces.connect(ws)
    try:
        row = conn.execute(
            "SELECT detail FROM foundry_events "
            "WHERE workspace_id = ? AND step = 'first_answer'",
            (ws,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    detail = json.loads(row[0])
    assert detail["agent"] == "d2c-librarian"
    assert detail["citations"] == n_citations
