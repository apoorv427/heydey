"""S4a: supervisor surface endpoints — Ask (evidence/full), Find, Models panel
(family rule blocked AT SAVE — the gate), costs ledger, reveal (Personal-walled).

Hermetic: embedding stubbed, pipeline monkeypatched for /ask full, subprocess
monkeypatched for /reveal. The real end-to-end path is proven by s4a_gate."""

import dataclasses

import pytest

from heydey import ask, models_config, pipeline, secrets_store, server, vector_store, workspaces

from conftest import auth_headers

DIM = 384


def _unit_vector(axis: int) -> list[float]:
    vec = [0.0] * DIM
    vec[axis] = 1.0
    return vec


@pytest.fixture()
def seeded_ws(heydey_home, monkeypatch):
    workspaces.create_workspace("s4aws")
    conn = workspaces.connect("s4aws")
    vector_store.upsert_points(conn, [
        ("p-pricing", _unit_vector(0), {
            "doc_id": "d-pricing", "text": "Orion pricing floor is 25L with AMC separate.",
            "source_file": "pricing-memo.md", "chunk_index": 3, "created_at": "2026-06-23T10:00:00",
        }),
        ("p-timeline", _unit_vector(1), {
            "doc_id": "d-timeline", "text": "Effective timeline is about two months; revenue-first sprint.",
            "source_file": "timeline-memo.md", "chunk_index": 1, "created_at": "2026-07-10T10:00:00",
        }),
    ])
    conn.execute(
        "INSERT INTO costs (run_id, model, tokens_in, tokens_out, cost_usd, latency_ms, created_at)"
        " VALUES ('r1','llama3.1:8b',100,50,0.0,900,datetime('now'))"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(ask, "embed_texts", lambda texts: [_unit_vector(0) for _ in texts])
    yield "s4aws"


def test_ask_requires_token(client, seeded_ws):
    response = client.post("/ask", json={"question": "x", "workspace": seeded_ws})
    assert response.status_code == 401


def test_ask_evidence_is_cited_and_fast(client, seeded_ws):
    response = client.post(
        "/ask",
        json={"question": "what is the pricing floor?", "workspace": seeded_ws},
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "evidence"
    assert body["citations"], "evidence mode must carry citations"
    top = body["citations"][0]
    assert top["source"] == "pricing-memo.md" and top["chunk"] == 3  # breadcrumb
    assert body["preview"].startswith("Orion pricing")
    assert body["latency_ms"] < 5000  # instant lane — no LLM call in this path
    assert body["profile"] == "local-only"


def test_ask_full_returns_pipeline_contract(client, seeded_ws, monkeypatch):
    fake = pipeline.RunResult(
        run_id="run-x", question="q", answer="cited answer", answer_kind="synthesized",
        validator_status="validated", validator_pass=True, executor_model="llama3.1:8b",
        validator_model="qwen3:8b", badge="llama3.1:8b → qwen3:8b · PASS",
        citations=[{"source": "pricing-memo.md"}], receipts=[{"sentence_index": 0}],
        ungrounded_count=0, cost_usd=0.0, duration_s=1.2, retry_used=False,
        hits=[{"payload": {"text": "SHOULD BE STRIPPED"}}],
    )
    monkeypatch.setattr(server.pipeline, "run_pipeline", lambda *a, **k: fake)
    response = client.post(
        "/ask",
        json={"question": "q", "workspace": seeded_ws, "mode": "full"},
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["badge"] == "llama3.1:8b → qwen3:8b · PASS"
    assert body["answer_kind"] == "synthesized" and body["validator_pass"] is True
    assert "hits" not in body, "full chunk payloads must stay server-side"


def test_find_groups_by_document(client, seeded_ws):
    response = client.post(
        "/find", json={"query": "pricing floor", "workspace": seeded_ws}, headers=auth_headers()
    )
    assert response.status_code == 200
    docs = response.json()["documents"]
    assert docs and docs[0]["source"] == "pricing-memo.md"
    assert docs[0]["chunks"][0]["chunk"] == 3  # breadcrumb survives grouping


def test_models_get_shows_profiles_and_key_presence(client, heydey_home, monkeypatch):
    monkeypatch.setattr(server.secrets_store, "get_secret", lambda name, **k: None)
    response = client.get("/models")
    assert response.status_code == 200
    body = response.json()
    assert body["active"] == "local-only"
    assert set(body["profiles"]) == {"local-only", "balanced", "quality-first"}
    lo = body["profiles"]["local-only"]["default"]
    assert lo["executor_family"] != lo["validator_family"]
    assert body["keys"] == {"openrouter": False}  # presence only, never the value


def test_models_same_family_blocked_at_save(client, heydey_home):
    """THE S4a misconfigure gate: a same-family pair must be refused with 422 at save."""
    response = client.put(
        "/models",
        json={
            "action": "save",
            "profile_data": {
                "name": "broken",
                "default": {"executor": "llama3.1:8b", "validator": "llama3.2:3b"},
                "budget_usd": 0.0,
            },
        },
        headers=auth_headers(),
    )
    assert response.status_code == 422
    assert "family" in response.json()["detail"]
    assert not (heydey_home / "models" / "broken.json").exists(), "never reaches disk"


def test_models_activate_persists_and_rejects_unknown(client, heydey_home):
    ok = client.put(
        "/models", json={"action": "activate", "profile": "balanced"}, headers=auth_headers()
    )
    assert ok.status_code == 200 and ok.json()["active"] == "balanced"
    assert client.get("/models").json()["active"] == "balanced"
    bad = client.put(
        "/models", json={"action": "activate", "profile": "nope"}, headers=auth_headers()
    )
    assert bad.status_code == 422


def test_set_key_stores_without_echo(client, heydey_home, monkeypatch):
    stored = {}
    monkeypatch.setattr(
        server.secrets_store, "set_secret", lambda name, value: stored.update({name: value})
    )
    response = client.put(
        "/models",
        json={"action": "set_key", "provider": "openrouter", "key": "sk-or-test-1234567890"},
        headers=auth_headers(),
    )
    assert response.status_code == 200
    assert response.json() == {"stored": True, "provider": "openrouter"}
    assert stored == {"OPENROUTER_API_KEY": "sk-or-test-1234567890"}
    assert "sk-or" not in response.text.replace("sk-or-test", "")  # no echo anywhere


def test_costs_ledger_shape(client, seeded_ws):
    response = client.get(f"/costs?workspace={seeded_ws}")
    assert response.status_code == 200
    body = response.json()
    assert body["today_calls"] >= 1 and body["week_calls"] >= 1
    assert body["recent"][0]["model"] == "llama3.1:8b"


def test_reveal_walls(client, heydey_home, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    outside = client.post("/reveal", json={"path": str(tmp_path / "x.md")}, headers=auth_headers())
    assert outside.status_code == 422 and not calls

    personal = server.reveal_roots()[0] / "Personal" / "note.md"
    walled = client.post("/reveal", json={"path": str(personal)}, headers=auth_headers())
    assert walled.status_code == 422 and not calls, "Personal wall applies to reveals"

    allowed = server.reveal_roots()[-1] / "CLAUDE.md"  # repo root is always a reveal root; CLAUDE.md exists
    ok = client.post("/reveal", json={"path": str(allowed)}, headers=auth_headers())
    assert ok.status_code == 200 and calls and calls[0][:2] == ["open", "-R"]


def test_run_result_is_json_serializable():
    """dataclasses.asdict over RunResult must stay JSON-clean (no exotic types)."""
    import json as _json

    result = pipeline.RunResult(
        run_id="r", question="q", answer="a", answer_kind="extractive",
        validator_status="validated", validator_pass=True, executor_model="e",
        validator_model="v", badge="b", citations=[], receipts=[], ungrounded_count=0,
        cost_usd=0.0, duration_s=0.1,
    )
    _json.dumps(dataclasses.asdict(result))
