"""Sentinel health flags (Contract B #4) + episodic run-log recall."""

from datetime import datetime, timedelta, timezone

import pytest

from heydey import episodic, sentinel, vector_store, workspaces


@pytest.fixture()
def conn(heydey_home):
    workspaces.create_workspace("senws")
    c = workspaces.connect("senws")
    yield c
    c.close()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


# ── test_sentinel_flags_stall — 24h graph zero-growth fires a Morning-Brief flag ─
def test_sentinel_flags_stall(conn):
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    conn.execute("INSERT INTO activity_edges(entity_a, entity_b, run_id, created_at) VALUES (1,2,'old',?)",
                 (_iso(stale),))
    conn.commit()
    report = sentinel.run_sentinel(conn)
    graph_check = next(c for c in report["checks"] if c["name"] == "graph_growth")
    assert graph_check["status"] == "flag"
    assert any("stall" in f.lower() for f in report["flags"])


def test_sentinel_fresh_graph_is_green(conn):
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    conn.execute("INSERT INTO activity_edges(entity_a, entity_b, run_id, created_at) VALUES (1,2,'new',?)",
                 (_iso(fresh),))
    conn.commit()
    report = sentinel.run_sentinel(conn)
    graph_check = next(c for c in report["checks"] if c["name"] == "graph_growth")
    assert graph_check["status"] == "ok"


def test_sentinel_flags_over_budget(conn):
    conn.execute("INSERT INTO costs(run_id, model, tokens_in, tokens_out, cost_usd, latency_ms, tier_reason, created_at) "
                 "VALUES ('r', 'm', 0, 0, 5.0, 1.0, 'x', ?)",
                 (_iso(datetime.now(timezone.utc)),))
    conn.commit()
    report = sentinel.run_sentinel(conn, budget_usd=1.0)
    cost_check = next(c for c in report["checks"] if c["name"] == "cost")
    assert cost_check["status"] == "flag"


def test_sentinel_flags_empty_store(conn):
    report = sentinel.run_sentinel(conn)
    retr = next(c for c in report["checks"] if c["name"] == "retrieval")
    assert retr["status"] == "flag"  # 0 points -> retrieval cannot answer


# ── episodic run-log ──────────────────────────────────────────────────────────
def test_record_and_recall(conn):
    episodic.record_run(conn, "run-1", "ORION pricing negotiation posture", duration=1.2, cost=0.0)
    episodic.record_run(conn, "run-2", "timeline cashflow triage", duration=0.8, cost=0.0)
    hits = episodic.recall(conn, "what did we decide about ORION pricing?", n=2)
    assert hits and hits[0]["run_id"] == "run-1"  # keyword overlap ranks pricing run first


def test_recall_is_idempotent_on_run_id(conn):
    episodic.record_run(conn, "run-x", "first intent", duration=1.0, cost=0.0)
    episodic.record_run(conn, "run-x", "updated intent", duration=2.0, cost=0.0)
    rows = conn.execute("SELECT COUNT(*) FROM sessions WHERE id='run-x'").fetchone()[0]
    assert rows == 1  # upsert, not duplicate


# ── S5 core: Session Browser substrate ────────────────────────────────────────

def test_record_run_scrubs_pii_at_write(conn):
    episodic.record_run(conn, "run-pii", "call me at 9876543210 about the quote",
                        duration=1.0, cost=0.0)
    intent = conn.execute("SELECT intent FROM sessions WHERE id='run-pii'").fetchone()[0]
    assert "9876543210" not in intent and "[REDACTED-PII]" in intent


def test_search_ranks_by_intent_overlap(conn):
    episodic.record_run(conn, "run-a", "cg police pricing quote", duration=1.0, cost=0.0)
    episodic.record_run(conn, "run-b", "timeline posture check", duration=1.0, cost=0.0)
    episodic.record_run(conn, "run-c", "hub71 deck review", duration=1.0, cost=0.0)
    conn.execute("INSERT INTO receipts(run_id, sentence_index, claim_text, created_at)"
                 " VALUES ('run-a', 0, 'quote is 12-18L', '2026-07-19')")
    conn.commit()

    hits = episodic.search(conn, "police quote")
    assert hits[0]["run_id"] == "run-a"
    assert hits[0]["receipts"] == 1  # the card can say what it can prove
    everything = episodic.search(conn, "")
    assert len(everything) >= 3  # empty query = recency stream


def test_session_detail_and_delete_forgets(conn):
    episodic.record_run(conn, "run-d", "beatpol amc question", duration=1.0, cost=0.0)
    conn.execute("INSERT INTO receipts(run_id, sentence_index, claim_text, created_at)"
                 " VALUES ('run-d', 0, 'amc is 18 percent', '2026-07-19')")
    conn.commit()

    detail = episodic.session_detail(conn, "run-d")
    assert detail["intent"] == "beatpol amc question"
    assert detail["receipts"][0]["claim_text"] == "amc is 18 percent"

    assert episodic.delete_session(conn, "run-d") is True
    assert episodic.session_detail(conn, "run-d") is None
    assert conn.execute("SELECT COUNT(*) FROM receipts WHERE run_id='run-d'").fetchone()[0] == 0
    assert episodic.delete_session(conn, "run-d") is False  # already forgotten
