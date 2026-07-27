"""AI-PM playbook (playbook_pm) — the USE-CASES W1 gate, made falsifiable:

  - PM-corpus failure-query pair -> correct chunk in top-3 with a breadcrumb
  - the Morning-Brief ``pm`` section cites >=3 distinct sources
  - the PRD-section prepared action is write_local end to end (tray -> local
    markdown artifact with verbatim quotes -> receipt row carrying the tier)
  - Foundry mints the spec'd trio (competitor-watch / user-voice / roadmap-risk)

The corpus routes in through a W3-B5 per-workspace block (dogfooding the
isolation rail). Embeddings are keyword-routed axis vectors so hybrid
retrieval is deterministic under test — the wiring is under test, not the
embedder."""

import json
import os

import pytest

from heydey import (approvals, ask, foundry, morning_brief, ops_ingest,
                    playbook_pm, vector_store, workspaces)
from heydey import config


def _axis_embed(texts):
    """friction -> axis 1, pricing -> axis 2, else axis 0 — query and chunk
    land on the same axis, so vector search agrees with the keyword lane."""
    out = []
    for t in texts:
        v = [0.0] * 384
        low = t.lower()
        if "friction" in low:
            v[1] = 1.0
        elif "pricing" in low:
            v[2] = 1.0
        else:
            v[0] = 1.0
        out.append(v)
    return out


@pytest.fixture()
def pm_ws(heydey_home, tmp_path, monkeypatch):
    """Three theme folders -> corpus.json workspaces block -> ingested 'pm'."""
    root = tmp_path / "pm-docs"
    (root / "interviews").mkdir(parents=True)
    (root / "competitor-notes").mkdir()
    (root / "prds").mkdir()
    (root / "interviews" / "2026-07-batch.md").write_text(
        "Users hit onboarding friction at the API-key step; four of ten quit there."
    )
    (root / "competitor-notes" / "rival-watch.md").write_text(
        "Rival Corp announced a pricing change to $99 per seat effective August."
    )
    (root / "prds" / "checkout-roadmap.md").write_text(
        "Checkout revamp milestone lands Q3; dependency on the payments team."
    )

    cfg = config.corpus_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "workspaces": {"pm": {"sources": [{"root": str(root), "glob": "**/*.md"}]}}
    }))

    monkeypatch.setattr(vector_store, "embed_texts", _axis_embed)
    monkeypatch.setattr(ask, "embed_texts", _axis_embed)

    workspaces.create_workspace("pm")
    report = ops_ingest.ingest_ops_corpus("pm")
    assert report["files"] == 3 and report["errors"] == []
    conn = workspaces.connect("pm")
    yield conn
    conn.close()


# ── the failure-query pair (S2 discipline on the PM corpus) ──────────────────

def test_failure_query_pair_lands_top3_with_breadcrumb(pm_ws):
    hits = ask.retrieve(pm_ws, "What did users say about onboarding friction?", k=3)
    sources = [h["payload"].get("source_file", "") for h in hits]
    assert any(s.endswith("2026-07-batch.md") for s in sources)

    hits = ask.retrieve(pm_ws, "What competitor pricing change happened?", k=3)
    top = [h["payload"] for h in hits]
    assert any(p.get("source_file", "").endswith("rival-watch.md") for p in top)
    assert all(p.get("source_file") for p in top)  # every hit carries its breadcrumb


# ── the brief section ─────────────────────────────────────────────────────────

def test_brief_cites_three_distinct_sources(pm_ws):
    items = playbook_pm.brief_section(pm_ws, "pm")
    assert len(items) >= 3
    assert all(i["section"] == "pm" for i in items)
    assert len({i["breadcrumb"]["source"] for i in items}) >= 3
    assert all(i["breadcrumb"]["date"] for i in items)


def test_brief_empty_when_corpus_is_stale(pm_ws, tmp_path):
    stale = 10 * 24 * 3600
    root = tmp_path / "pm-docs"
    for path in root.rglob("*.md"):
        old = path.stat().st_mtime - stale
        os.utime(path, (old, old))
    ops_ingest.ingest_ops_corpus("pm")  # idempotent re-ingest, old mtimes
    assert playbook_pm.brief_section(pm_ws, "pm") == []


def test_build_brief_registers_pm_section(pm_ws):
    items = morning_brief.build_brief(pm_ws, workspace_id="pm")
    assert any(i["section"] == "pm" for i in items)


# ── the prepared action (write_local end to end) ─────────────────────────────

def test_prd_section_action_end_to_end(pm_ws):
    approval_id = playbook_pm.prd_section_approval(pm_ws, "pm", "onboarding friction")
    assert approval_id is not None

    row = approvals.pending(pm_ws)[0]
    assert row["risk_tier"] == "write_local"
    assert row["payload"]["kind"] == "prd_section"
    assert row["payload"]["sources"]  # evidence named before approval

    result = approvals.decide(pm_ws, approval_id, "approved", workspace_id="pm")
    assert result["risk_tier"] == "write_local"
    artifact = result["artifact"]
    text = open(artifact).read()
    assert "PRD section draft — onboarding friction" in text
    assert "> " in text and "## Sources" in text
    assert "2026-07-batch.md" in text  # the verbatim quote's source line

    tier = pm_ws.execute(
        "SELECT risk_tier FROM receipts WHERE run_id = ?",
        (f"approval-{approval_id}",)).fetchone()[0]
    assert tier == "write_local"


def test_prd_action_is_silent_without_evidence(heydey_home, monkeypatch):
    monkeypatch.setattr(ask, "embed_texts", _axis_embed)
    workspaces.create_workspace("pm-empty")
    conn = workspaces.connect("pm-empty")
    try:
        assert playbook_pm.prd_section_approval(conn, "pm-empty", "anything") is None
        assert playbook_pm.prd_section_approval(conn, "pm-empty", "  ") is None
    finally:
        conn.close()


# ── the Foundry trio ──────────────────────────────────────────────────────────

def test_foundry_mints_the_pm_trio(pm_ws):
    fleet = foundry.instantiate(pm_ws, "pm", {
        "business_type": "product",
        "company_name": "DEMO ProductCo",
        "primary_goal": "daily_brief",
        "sources": ["prds", "interviews", "competitor-notes"],
        "answer_style": "synthesized",
    })
    ids = {s["id"] for s in fleet}
    assert ids == {"pm-competitor-watch", "pm-user-voice", "pm-roadmap-risk"}
    assert all(s["playbook"] == "pm-product" for s in fleet)
    assert all(s["validator_pass"] == 1 for s in fleet)


def test_foundry_refuses_theme_with_no_docs(heydey_home, monkeypatch):
    monkeypatch.setattr(vector_store, "embed_texts", _axis_embed)
    workspaces.create_workspace("pm-bare")
    conn = workspaces.connect("pm-bare")
    try:
        with pytest.raises(foundry.FoundryError, match="no chunks from"):
            foundry.instantiate(conn, "pm-bare", {
                "business_type": "product",
                "company_name": "DEMO ProductCo",
                "primary_goal": "daily_brief",
                "sources": ["interviews"],
                "answer_style": "verbatim",
            })
    finally:
        conn.close()
