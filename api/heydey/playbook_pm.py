"""AI-PM Playbook — the first DOC-driven vertical (post-app W1 spec, USE-CASES).

Where ``playbook_d2c`` reads guarded connector rows out of ``mcp_results``,
this one reads the PM's own ingested corpus (PRDs, user interviews, competitor
notes — a per-workspace corpus block routes the folders in) straight off the
``points`` store. Two outputs, mirroring the d2c structure:

  - a Morning-Brief ``pm`` section: which corpus themes moved in the window —
    one line per new/updated doc, each breadcrumbed to its source file. No
    docs in the window -> [] (the section does not render; never a fake line).
  - a prepared PRD-SECTION approval: top retrieved chunks for a topic, packed
    VERBATIM into a tray payload. Approving writes a local markdown artifact
    (quotes + per-quote source lines + a sources footer) — ``write_local``
    tier, no write fires, exactly the prepared-action pattern.

Deterministic by design: SQL + arithmetic + existing hybrid retrieval — no LLM
call on either path (Contract B bans LLM-in-cron; retrieval is deterministic
given the store). Extraction only: the artifact quotes chunks, it never
synthesizes — a PRD *draft skeleton with receipts*, not generated prose.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import approvals, ask, risk

SECTION = "pm"
MAX_DOC_LINES = 6

# Theme = folder-shape heuristic on the source path. Order matters: first hit
# wins; "other" is the honest bucket for anything unrecognized.
_THEMES = (
    ("interviews", ("interview",)),
    ("competitor-notes", ("competitor",)),
    ("prds", ("prd", "roadmap")),
)


def _theme_of(source_file: str) -> str:
    lowered = source_file.lower()
    for theme, needles in _THEMES:
        if any(n in lowered for n in needles):
            return theme
    return "other"


def recent_docs(conn: sqlite3.Connection, window_hours: int = 48) -> list[dict]:
    """New/updated corpus docs inside the window, newest first — straight off
    ``points`` (doc-level MAX(created_at); created_at is the file's mtime)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)) \
        .isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT doc_id, COALESCE(source_file, doc_id), MAX(created_at)"
        " FROM points GROUP BY doc_id HAVING MAX(created_at) >= ?"
        " ORDER BY MAX(created_at) DESC",
        (cutoff,),
    ).fetchall()
    return [{"doc_id": r[0], "source_file": r[1], "changed_at": r[2] or "",
             "theme": _theme_of(r[1])} for r in rows]


def brief_section(conn: sqlite3.Connection, workspace_id: str) -> list[dict]:
    """Morning-Brief ``pm`` items (same shape as every section). One line per
    moved doc, capped at MAX_DOC_LINES with an honest rollup line beyond."""
    docs = recent_docs(conn)
    if not docs:
        return []
    items = [
        {"section": SECTION,
         "line": f"{d['theme']}: new/updated — {Path(d['source_file']).name}",
         "breadcrumb": {"source": d["source_file"], "chunk": None,
                        "date": (d["changed_at"] or "")[:10]}}
        for d in docs[:MAX_DOC_LINES]
    ]
    if len(docs) > MAX_DOC_LINES:
        items.append({
            "section": SECTION,
            "line": f"+{len(docs) - MAX_DOC_LINES} more docs changed in the window",
            "breadcrumb": {"source": docs[MAX_DOC_LINES]["source_file"],
                           "chunk": None,
                           "date": (docs[MAX_DOC_LINES]["changed_at"] or "")[:10]},
        })
    return items


def prd_section_approval(conn: sqlite3.Connection, workspace_id: str,
                         topic: str, k: int = 6) -> int | None:
    """Build the PRD-section tray payload from hybrid retrieval over the PM
    corpus. Returns the approval id, or None when nothing retrievable supports
    the topic (cite-or-silent applies to actions too — no evidence, no card)."""
    topic = (topic or "").strip()
    if not topic:
        return None
    hits = ask.retrieve(conn, topic, k=k)
    if not hits:
        return None

    quotes = []
    for hit in hits:
        payload = hit.get("payload", {})
        text = (payload.get("text") or "").strip()
        if not text:
            continue
        quotes.append({
            "text": text[:400],
            "source": payload.get("source_path") or payload.get("source_file")
                      or payload.get("title") or "unknown",
            "chunk": payload.get("chunk_index", 0),
            "score": round(hit["vscore"], 3) if hit.get("vscore") is not None else None,
        })
    if not quotes:
        return None

    payload = {
        "kind": "prd_section",
        "title": f"Draft PRD section: {topic}?",
        "topic": topic,
        "quotes": quotes,
        "sources": sorted({q["source"] for q in quotes}),
    }
    # prepared action = a markdown draft on this machine only -> write_local
    return approvals.create_approval(conn, action_class="outbound", payload=payload,
                                     risk_tier=risk.WRITE_LOCAL)
