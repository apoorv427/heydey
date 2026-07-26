"""Graph extraction engine (Executor Contract B) — the anti-LightRAG contract.

A living entity/relationship graph that is a **byproduct of normal use**, never a
scheduled job — so it cannot silently die the way LightRAG did (45+ days of silent
nightly-batch failure, still erroring in the old STATUS). Pure SQLite, event-driven:

- **entities @ ingest** — :func:`index_document` parses each doc for entities with a
  regex-first pass (project names, ALLCAPS markers like PENDING/GATE/LOCKED, lock ids
  L##, decision keywords, Title-Case proper nouns). Per-document error isolation: one
  bad doc logs + skips, the pipeline never aborts. Optional Ollama classify for
  low-confidence spans is a hook (off by default; the ingest CLI may enable it) — and
  even then it is per-item, inline, never a cron batch.
- **edges @ query** — :func:`record_coretrieval` writes activity_edges between the
  entities of co-retrieved docs. Co-retrieval *is* the relationship signal; no LLM call.
- **health** — :func:`health` reports entity/edge counts + last-grown; 24h zero-growth
  is the Sentinel's Morning-Brief flag.
- **render** — :func:`top_entities` returns the top-N by activity score for the
  read-only D3 panel (built at S4, gated behind a proven S2 corpus).

BANNED here (CI-guarded, test_banned_deps): neo4j / networkx imports; any LLM call
inside a scheduled batch without per-item error handling + a Sentinel signal.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

# Curated project / product vocabulary — the entities ops questions actually
# turn on. Installation-specific names are MACHINE-LOCAL state (corpus.json
# `graph_seed`, merged over a minimal default) — never repo literals. Tests and
# embedders may set the module attribute directly.
DEFAULT_PROJECTS = ["Heydey"]
KNOWN_PROJECTS: list[str] | None = None  # None -> corpus.json graph_seed + defaults


def _project_names() -> list[str]:
    names = KNOWN_PROJECTS
    if names is None:
        from . import config
        names = [*config.load_corpus_config().get("graph_seed", []), *DEFAULT_PROJECTS]
    return list(dict.fromkeys(names)) or DEFAULT_PROJECTS


_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("marker", re.compile(r"\b(PENDING|GATE|BLOCKER|BLOCKED|LOCKED|LOCK|TODO|DECISION|GREEN|RED)\b")),
    ("lock", re.compile(r"\bL\d{1,3}\b")),
    ("slice", re.compile(r"\bS\d[a-c]?\b")),
    ("money", re.compile(r"₹\s?\d[\d,.]*\s?(?:L|Cr|crore|lakh|k)?", re.IGNORECASE)),
]


def _all_patterns() -> list[tuple[str, re.Pattern]]:
    project = re.compile(r"\b(" + "|".join(re.escape(p) for p in _project_names()) + r")\b")
    return [*_PATTERNS, ("project", project)]

# Title-Case proper-noun runs (people, orgs) — 1-3 capitalised words, not sentence-start noise.
_PROPER = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b")
_STOP_PROPER = {
    "The", "This", "That", "These", "Those", "It", "We", "They", "Read", "Build",
    "Every", "Both", "Note", "Next", "First", "When", "Where", "What", "Status",
    "Owner", "Last", "Phase", "None", "Personal",
}
_ISO = "%Y-%m-%dT%H:%M:%S"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_entities(text: str) -> list[dict]:
    """Regex-first entity extraction. Returns [{label, type, confidence}] de-duped."""
    found: dict[tuple[str, str], float] = {}

    def add(label: str, etype: str, conf: float) -> None:
        label = label.strip()
        if etype == "money":
            # "₹12 L" / "₹12L" / "₹18," were three nodes in the live panel —
            # normalise money to one canonical spelling before identity forms
            label = re.sub(r"[\s,.]+$", "", label)
            label = re.sub(r"\s+", "", label)
        if len(label) < 2 or len(label) > 60:
            return
        key = (label, etype)
        found[key] = max(found.get(key, 0.0), conf)

    for etype, pat in _all_patterns():
        for m in pat.findall(text):
            label = m if isinstance(m, str) else next((g for g in m if g), "")
            add(label, etype, 0.95)  # curated/regex hits are high-confidence

    for m in _PROPER.findall(text):
        head = m.split()[0]
        if head in _STOP_PROPER:
            continue
        # a single common capitalised word is likely sentence-start noise; require
        # either a multi-word run or membership in the known vocabulary.
        conf = 0.8 if " " in m else 0.55
        add(m, "proper_noun", conf)

    return [{"label": l, "type": t, "confidence": round(c, 2)} for (l, t), c in found.items()]


def index_document(conn: sqlite3.Connection, doc_id: str, text: str,
                   workspace_id: str, *, min_conf: float = 0.55) -> int:
    """Extract + persist a doc's entities (idempotent per doc). Returns count written.

    Per-document error isolation is the CALLER's contract, but we also guard here so a
    single malformed doc can never corrupt the graph: on any parse error we skip and
    return 0 rather than raise.
    """
    try:
        ents = [e for e in extract_entities(text) if e["confidence"] >= min_conf]
    except Exception:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        # IDENTITY-PRESERVING sync (S4c regression: delete+reinsert minted new rowids
        # on every re-ingest, orphaning EVERY activity_edge — 1,344 dead edges found
        # live). An entity that survives a re-ingest keeps its id, so edge history
        # stays attached; only truly-removed entities (and their edges) are dropped.
        existing = {(r[1], r[2]): r[0] for r in conn.execute(
            "SELECT id, label, type FROM entities WHERE source_doc_id=?", (doc_id,))}
        fresh = {(e["label"], e["type"]): e for e in ents}

        gone_ids = [existing[key] for key in existing.keys() - fresh.keys()]
        if gone_ids:
            marks = ",".join("?" * len(gone_ids))
            conn.execute(f"DELETE FROM entities WHERE id IN ({marks})", gone_ids)
            # edge hygiene at the source — never leave dangling endpoints behind
            conn.execute(
                f"DELETE FROM activity_edges WHERE entity_a IN ({marks}) "
                f"OR entity_b IN ({marks})", (*gone_ids, *gone_ids))

        for key in existing.keys() & fresh.keys():
            conn.execute("UPDATE entities SET confidence=? WHERE id=?",
                         (fresh[key]["confidence"], existing[key]))

        now = _now()
        conn.executemany(
            "INSERT INTO entities(label, type, workspace_id, source_doc_id, confidence, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [(e["label"], e["type"], workspace_id, doc_id, e["confidence"], now)
             for key, e in fresh.items() if key not in existing],
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return 0
    return len(ents)


def _entity_ids_for_docs(conn: sqlite3.Connection, doc_ids: list[str], cap: int) -> list[int]:
    if not doc_ids:
        return []
    marks = ",".join("?" * len(doc_ids))
    rows = conn.execute(
        f"SELECT id FROM entities WHERE source_doc_id IN ({marks}) "
        f"ORDER BY confidence DESC LIMIT ?", (*doc_ids, cap)
    ).fetchall()
    return [r[0] for r in rows]


def record_coretrieval(conn: sqlite3.Connection, run_id: str, hits: list[dict],
                       *, max_entities: int = 8) -> int:
    """Write activity_edges between entities of the co-retrieved docs. No LLM call.

    The relationship signal is "these appeared together in one answer this week."
    Bounded to the top ``max_entities`` (by confidence) across the retrieved docs so a
    wide result set can't explode into O(n^2) edges.
    """
    doc_ids = []
    for h in hits:
        payload = h.get("payload", h)
        did = payload.get("doc_id") or payload.get("source_path") or payload.get("source_file")
        if did and did not in doc_ids:
            doc_ids.append(did)
    ids = _entity_ids_for_docs(conn, doc_ids, max_entities)
    if len(ids) < 2:
        return 0
    ts = _now()
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
    conn.executemany(
        "INSERT INTO activity_edges(entity_a, entity_b, run_id, created_at) VALUES (?,?,?,?)",
        [(a, b, run_id, ts) for a, b in pairs],
    )
    conn.commit()
    return len(pairs)


def mine_relationships(conn: sqlite3.Connection) -> int:
    """Fold activity_edges into weighted relationships at read-time (co-occurrence count).
    Not written by any scheduler — called on demand (Sentinel / graph panel refresh)."""
    rows = conn.execute(
        "SELECT entity_a, entity_b, COUNT(*), MAX(created_at) FROM activity_edges "
        "GROUP BY entity_a, entity_b"
    ).fetchall()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM relationships")
        conn.executemany(
            "INSERT INTO relationships(src_id, dst_id, rel_type, weight, last_seen) "
            "VALUES (?,?, 'co_activity', ?, ?)",
            [(a, b, float(w), seen) for a, b, w, seen in rows],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def health(conn: sqlite3.Connection) -> dict:
    """Graph vitals for the Sentinel + Dev Mode line: counts, last-grown, 24h stall."""
    ents = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM activity_edges").fetchone()[0]
    last = conn.execute("SELECT MAX(created_at) FROM activity_edges").fetchone()[0]
    stalled = True
    if last:
        try:
            then = datetime.strptime(last[:19], _ISO).replace(tzinfo=timezone.utc)
            stalled = (datetime.now(timezone.utc) - then) > timedelta(hours=24)
        except ValueError:
            stalled = False
    return {"entities": ents, "edges": edges, "last_grown": last,
            "stalled_24h": bool(stalled and edges > 0) or (edges == 0)}


def top_entities(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Top-N entities by activity score (edge degree), for the read-only D3 panel.

    Live-only by construction: degree counts join against ``entities``, so a
    dangling edge endpoint can never surface as ``<unknown>`` in the panel."""
    deg: Counter = Counter()
    live = {r[0] for r in conn.execute("SELECT id FROM entities").fetchall()}
    for a, b in conn.execute("SELECT entity_a, entity_b FROM activity_edges").fetchall():
        if a in live and b in live:
            deg[a] += 1
            deg[b] += 1
    if not deg:
        # cold graph: fall back to highest-confidence entities so the panel isn't blank
        rows = conn.execute(
            "SELECT id, label, type, confidence FROM entities ORDER BY confidence DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"id": r[0], "label": r[1], "type": r[2], "score": 0, "confidence": r[3]} for r in rows]
    top_ids = [eid for eid, _ in deg.most_common(limit)]
    marks = ",".join("?" * len(top_ids))
    meta = {r[0]: (r[1], r[2]) for r in
            conn.execute(f"SELECT id, label, type FROM entities WHERE id IN ({marks})", top_ids)}
    return [{"id": eid, "label": meta[eid][0], "type": meta[eid][1], "score": score}
            for eid, score in deg.most_common(limit)]


def panel(conn: sqlite3.Connection, limit: int = 50) -> dict:
    """The read-only D3 panel payload: top-N live nodes + the edges among them
    (aggregated from activity_edges, weight = co-activity count) + health line.

    The 50-node perf cap is enforced HERE, not in the HTTP layer — the S4c gate
    calls this function directly with limit=999 to prove no caller can widen it."""
    nodes = top_entities(conn, min(limit, 50))
    ids = {n["id"] for n in nodes}
    weights: Counter = Counter()
    last_seen: dict[tuple[int, int], str] = {}
    for a, b, ts in conn.execute(
        "SELECT entity_a, entity_b, created_at FROM activity_edges"
    ).fetchall():
        if a in ids and b in ids:
            key = (a, b) if a <= b else (b, a)
            weights[key] += 1
            if ts and ts > last_seen.get(key, ""):
                last_seen[key] = ts
    edges = [{"source": a, "target": b, "weight": w, "last_seen": last_seen.get((a, b), "")}
             for (a, b), w in weights.most_common(400)]  # perf cap on the wire
    return {"nodes": nodes, "edges": edges, "health": health(conn)}


def entity_detail(conn: sqlite3.Connection, entity_id: int) -> dict | None:
    """Node click -> the entity + its receipts + the runs/sessions that touched it
    (Contract B render spec). Read-only."""
    row = conn.execute(
        "SELECT id, label, type, source_doc_id, confidence FROM entities WHERE id=?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    run_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT run_id FROM activity_edges WHERE entity_a=? OR entity_b=?"
        " ORDER BY created_at DESC LIMIT 10", (entity_id, entity_id)).fetchall()]
    sessions = []
    if run_ids:
        marks = ",".join("?" * len(run_ids))
        sessions = [dict(r) for r in conn.execute(
            f"SELECT id, intent, started_at, duration, cost FROM sessions"
            f" WHERE id IN ({marks}) ORDER BY started_at DESC", run_ids).fetchall()]
    receipts = [dict(r) for r in conn.execute(
        "SELECT run_id, claim_text, chunk_id, retrieval_score, confidence_band, created_at"
        " FROM receipts WHERE doc_id=? ORDER BY id DESC LIMIT 5",
        (row[3],)).fetchall()]
    return {"id": row[0], "label": row[1], "type": row[2], "source_doc_id": row[3],
            "confidence": row[4], "runs": run_ids, "sessions": sessions, "receipts": receipts}
