"""Episodic run-log — "recall any prior work session by intent" (11/10 def, item d).

Every pipeline run is recorded to the ``sessions`` table (id, intent, started_at,
duration, cost, project, workspace_id). :func:`recall` injects the last N relevant
runs by keyword overlap on intent — the substrate for the Session Browser (L25) and
for the "why did this agent do X" recall that solves agent-amnesia.

Deliberately dumb: keyword overlap, not a vector index. Summary embeddings can be
added later into the reserved ``summary_embedding`` column; for S3 the honest,
inspectable version is a LIKE/word-overlap match.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from .ingest_guard import scrub_pii


def record_run(conn: sqlite3.Connection, run_id: str, intent: str, *, duration: float,
               cost: float, project: str = "", workspace_id: str = "") -> None:
    """Append one run to the episodic log (idempotent on run_id).

    The intent is PII-scrubbed AT WRITE (S5: the Session Browser renders these
    verbatim — a phone number typed into an Ask must never reach that surface)."""
    intent, _ = scrub_pii(intent or "")
    conn.execute(
        "INSERT INTO sessions(id, intent, started_at, duration, cost, project, workspace_id) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "intent=excluded.intent, duration=excluded.duration, cost=excluded.cost",
        (run_id, intent, datetime.now(timezone.utc).isoformat(timespec="seconds"),
         duration, cost, project, workspace_id),
    )
    conn.commit()


def recall(conn: sqlite3.Connection, intent: str, n: int = 3) -> list[dict]:
    """Last N prior runs most relevant to ``intent`` (word overlap, recency tiebreak)."""
    terms = {t for t in re.findall(r"\w{3,}", (intent or "").lower())}
    rows = conn.execute(
        "SELECT id, intent, started_at, duration, cost, project FROM sessions "
        "ORDER BY started_at DESC LIMIT 200"
    ).fetchall()
    scored = []
    for r in rows:
        words = set(re.findall(r"\w{3,}", (r[1] or "").lower()))
        overlap = len(terms & words)
        if overlap or not terms:
            scored.append((overlap, r))
    scored.sort(key=lambda x: (x[0], x[1][2]), reverse=True)
    return [
        {"run_id": r[0], "intent": r[1], "at": r[2], "duration": r[3], "cost": r[4], "project": r[5]}
        for _, r in scored[:n]
    ]


def recent(conn: sqlite3.Connection, n: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT id, intent, started_at, duration, cost FROM sessions "
        "ORDER BY started_at DESC LIMIT ?", (n,)
    ).fetchall()
    return [{"run_id": r[0], "intent": r[1], "at": r[2], "duration": r[3], "cost": r[4]} for r in rows]


def search(conn: sqlite3.Connection, query: str = "", limit: int = 30) -> list[dict]:
    """The Session Browser's stream: sessions ranked by intent overlap (recency
    tiebreak; empty query = pure recency), each carrying its receipts count so a
    card can say what it can prove. Read-only."""
    terms = {t for t in re.findall(r"\w{3,}", (query or "").lower())}
    rows = conn.execute(
        "SELECT s.id, s.intent, s.started_at, s.duration, s.cost, s.project,"
        "       (SELECT COUNT(*) FROM receipts r WHERE r.run_id = s.id) AS receipts"
        " FROM sessions s ORDER BY s.started_at DESC LIMIT 500"
    ).fetchall()
    scored = []
    for r in rows:
        words = set(re.findall(r"\w{3,}", (r[1] or "").lower()))
        overlap = len(terms & words)
        if overlap or not terms:
            scored.append((overlap, r))
    scored.sort(key=lambda x: (x[0], x[1][2]), reverse=True)
    return [
        {"run_id": r[0], "intent": r[1], "at": r[2], "duration": r[3], "cost": r[4],
         "project": r[5], "receipts": r[6], "match": overlap}
        for overlap, r in scored[:limit]
    ]


def session_detail(conn: sqlite3.Connection, run_id: str) -> dict | None:
    """One session + its receipt lines — the card's drill-down."""
    row = conn.execute(
        "SELECT id, intent, started_at, duration, cost, project FROM sessions WHERE id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    receipts = [dict(r) for r in conn.execute(
        "SELECT sentence_index, claim_text, doc_id, retrieval_score, confidence_band,"
        "       validator_pass, model, created_at"
        " FROM receipts WHERE run_id=? ORDER BY sentence_index", (run_id,)).fetchall()]
    return {"run_id": row[0], "intent": row[1], "at": row[2], "duration": row[3],
            "cost": row[4], "project": row[5], "receipts": receipts}


def delete_session(conn: sqlite3.Connection, run_id: str) -> bool:
    """Delete a session AND its receipts (the user's right to forget a run).
    Returns False if the session doesn't exist. Edges keep their run_id strings
    (they reference entity activity, not the session row)."""
    exists = conn.execute("SELECT 1 FROM sessions WHERE id=?", (run_id,)).fetchone()
    if exists is None:
        return False
    conn.execute("DELETE FROM receipts WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (run_id,))
    conn.commit()
    return True
