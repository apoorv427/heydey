"""Ask engine — ported from the proven BLOS module (S1, lock L7).

"Ask your business anything -> cited, source-attributed answer with a clickable
breadcrumb to the exact chunk." Frontend-agnostic; every surface calls ask().

Retrieval is HYBRID over the workspace's SQLite file: semantic (sqlite-vec) +
keyword (FTS5), fused with Reciprocal Rank Fusion. Always works offline.

Answer synthesis is EXTRACTIVE by default (most relevant passage, verbatim, with
citations) — cite-or-silent honest, no LLM needed. LLM synthesis becomes available
when the router slice lands (HEYDEY_ASK_LLM=1 + heydey.llm_router); it is opt-in
and fails safe back to extractive. Receipts-per-sentence arrive with the S3
validator gate — until then answer_kind labels the exact provenance.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone

from . import vector_store
from .embedder import embed_texts

RRF_K = 60  # reciprocal-rank-fusion constant

# §4.1 scoring, calibrated for CHUNK-level retrieval (S2 sweep, measured):
# the vector list is pre-ranked by similarity x0.6 + recency x0.10, THEN
# RRF-fused with keywords — rank fusion stays load-bearing (dropping it broke
# an S2 gate query, measured). W_RECENCY amended 0.3 -> 0.10: chunk sim spreads
# are ~0.05-0.15, so 0.3 let the 6-hourly-rewritten STATUS.md flood every
# top-3 (measured regression). frequency activates with the receipts ledger
# (S3) — 0 until then. Full sweep table in the S2 commit message.
W_SIM, W_RECENCY, W_FREQ = 0.6, 0.10, 0.1
RECENCY_HALF_LIFE_DAYS = 90.0
POOL_MULTIPLIER = 8  # ops memos sit deep behind topical walls (measured: rank 31)
PER_DOC_CAP = 2  # diversity: one document may fill at most 2 of the k results

# BM25F-style title-field weight: a document whose TITLE carries a query term
# (≥4 chars, stopword-safe) is how ops recall actually works ("the pricing memo").
# Bounded: caps at 2 term hits; a title-only doc still needs body evidence.
TITLE_TERM_BONUS = 0.008


def _fuse(vector_hits, keyword_hits, limit):
    """Reciprocal Rank Fusion of the two ranked lists, keyed by point_id."""
    scores, payloads = {}, {}
    for rank, hit in enumerate(vector_hits):
        pid = hit["point_id"]
        scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
        payloads[pid] = {"payload": hit["payload"], "vscore": hit.get("score")}
    for rank, hit in enumerate(keyword_hits):
        pid = hit["point_id"]
        scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
        payloads.setdefault(pid, {"payload": hit["payload"], "vscore": None})
    ranked = sorted(scores, key=lambda p: scores[p], reverse=True)[:limit]
    return [{"point_id": p, "rrf": round(scores[p], 5), **payloads[p]} for p in ranked]


def _recency(payload: dict) -> float:
    """Exponential freshness decay (half-life 90d) from the chunk's timestamp.
    Undated content decays to 0 — old corpus without dates never outranks on recency."""
    stamp = (payload.get("mtime_iso") or payload.get("created_at") or "")[:10]
    try:
        then = datetime.strptime(stamp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    age_days = max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def retrieve(conn: sqlite3.Connection, question: str, k: int = 6) -> list[dict]:
    """Hybrid retrieve top-k chunks for a question from one workspace's store.

    Pipeline (each step earned by a measured failure, see constants above):
    vector top-k*8, pre-ranked by 0.6*sim + 0.10*recency -> RRF-fuse with
    keyword hits (OR-fallback) -> dedup -> per-doc cap -> top-k.
    """
    qvec = embed_texts([question])[0]
    pool = k * POOL_MULTIPLIER
    vector_hits = sorted(
        vector_store.search_by_vector(conn, qvec, limit=pool),
        key=lambda h: W_SIM * h["score"] + W_RECENCY * _recency(h["payload"]),
        reverse=True,
    )
    try:
        keyword_hits = vector_store.keyword_search(conn, question, limit=k * 4,
                                                   or_fallback=True)
    except Exception:
        keyword_hits = []
    fused = _fuse(vector_hits, keyword_hits, pool)

    # Dedup: the KB holds some duplicate points, so collapse by (source,
    # chunk_index) keeping the highest-ranked — else one passage cites 3x.
    seen, deduped = set(), []
    for hit in fused:
        payload = hit["payload"]
        # Contract C Layer 1: an injection-flagged chunk is stored for audit but
        # NEVER retrievable — retrieval is the one chokepoint every LLM-facing
        # path (ask, brief, playbook) passes through, so the exclusion lives here
        # rather than per-lane. Covers ops-ingested and connector-synced chunks
        # alike. (The mcp_results boundary already excludes flagged *results*;
        # this closes the *chunk* path, which had no exclusion before S6b.)
        if payload.get("injection_risk"):
            continue
        source = payload.get("source_path") or payload.get("source_file") or payload.get("title", "")
        key = (source, payload.get("chunk_index"), (payload.get("text", "") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        hit["_source"] = source
        deduped.append(hit)

    query_terms = {t.lower() for t in re.findall(r"\w{4,}", question)}
    for hit in deduped:
        title = str(hit["payload"].get("title") or os.path.basename(hit["_source"])).lower()
        title_hits = sum(1 for term in query_terms if term in title)
        hit["rrf"] = round(hit["rrf"] + TITLE_TERM_BONUS * min(title_hits, 2), 5)

    # Document-level evidence pooling (S2, measured): a doc whose vector-best
    # and keyword-best chunks DIFFER never sums in chunk-keyed RRF, so a
    # 6-chunk-relevant memo lost to one-hit noise. Rank documents by their
    # top-2 pooled chunk scores, then emit each doc's best chunks (cap 2).
    by_doc: dict[str, list[dict]] = {}
    for hit in deduped:
        by_doc.setdefault(hit["_source"], []).append(hit)
    doc_order = sorted(
        by_doc.values(),
        key=lambda hits: sum(h["rrf"] for h in hits[:2]),
        reverse=True,
    )
    out = []
    for doc_hits in doc_order:
        for hit in doc_hits[:PER_DOC_CAP]:
            hit.pop("_source", None)
            out.append(hit)
            if len(out) >= k:
                return out
    return out


def _citation(hit: dict) -> dict:
    payload = hit["payload"]
    date = (payload.get("mtime_iso") or payload.get("created_at") or "")[:10]
    return {
        "source": payload.get("source_path") or payload.get("source_file") or payload.get("title") or "unknown",
        "path": payload.get("source_path") or payload.get("source_file") or "",
        "chunk": payload.get("chunk_index", 0),
        "date": date,
        "score": round(hit["vscore"], 3) if hit.get("vscore") is not None else None,
        "snippet": (payload.get("text", "") or "")[:400],
        "flagged": bool(payload.get("injection_risk")),
    }


def find_docs(conn: sqlite3.Connection, query: str, k: int = 5) -> list[dict]:
    """Doc-level ranked search — the Library bar / ``blos find`` / ``/find`` share
    this one implementation. Groups chunk hits by source doc (first-hit rank wins,
    best score kept), top-2 chunks each as checkable breadcrumbs."""
    hits = retrieve(conn, query, k=max(k * 2, 12))
    docs: dict[str, dict] = {}
    for rank, hit in enumerate(hits):
        cite = _citation(hit)
        key = cite["path"] or cite["source"]
        entry = docs.setdefault(key, {
            "source": cite["source"], "path": cite["path"], "date": cite["date"],
            "best_score": cite["score"], "best_rank": rank, "chunks": [],
        })
        entry["chunks"].append({"chunk": cite["chunk"], "score": cite["score"],
                                "snippet": cite["snippet"][:200]})
        if (cite["score"] or 0) > (entry["best_score"] or 0):
            entry["best_score"] = cite["score"]
    ranked = sorted(docs.values(), key=lambda d: d["best_rank"])[:k]
    for entry in ranked:
        entry["chunks"] = entry["chunks"][:2]
        entry.pop("best_rank")
    return ranked


def _trim_verbatim(text: str, limit: int = 700) -> str:
    """Select a clean verbatim window: never start mid-word (chunk-overlap
    boundaries produce fragments like 'mpletion claims' — shipped to a user at
    S3 prompt 17), and end on a sentence boundary when one exists past half the
    window. Pure substring selection — grounding by construction is preserved."""
    t = text.strip()
    if t and (t[0].islower() or t[0] in ",;:)]}"):
        m = re.search(r"(?:[.!?]\s+)|\n", t[:220])
        if m:
            t = t[m.end():].lstrip()
    if len(t) > limit:
        t = t[:limit]
        boundary = max(t.rfind(". "), t.rfind(".\n"), t.rfind("! "), t.rfind("? "))
        if boundary > limit // 2:
            t = t[: boundary + 1]
    return t.strip()


def _extractive_answer(question: str, hits: list[dict]) -> str:
    """The most SEMANTICALLY relevant passage (highest vector score) — RRF rank-0
    can be a keyword hit from mid-document; the top-vector chunk reads more cleanly."""
    if not hits:
        return ""
    best = max(hits, key=lambda h: h["vscore"] if h.get("vscore") is not None else -1.0)
    return _trim_verbatim(best["payload"].get("text", ""))


def _llm_answer(question: str, hits: list[dict]) -> str | None:
    """Grounded synthesis via the router. Opt-in (HEYDEY_ASK_LLM=1); fails safe.
    The router module lands at S4 — until then this always returns None."""
    if os.getenv("HEYDEY_ASK_LLM") != "1":
        return None
    try:
        from heydey.llm_router import route_completion  # type: ignore[import-not-found]
        context = "\n\n".join(
            f"[{i + 1}] ({h['payload'].get('source_path') or h['payload'].get('source_file', '?')}): "
            f"{h['payload'].get('text', '')[:800]}"
            for i, h in enumerate(hits)
        )
        prompt = (
            "Answer the question using ONLY the numbered context below. Cite sources inline "
            "as [1], [2]. If the context does not answer it, say 'The KB doesn't cover this yet.'\n\n"
            f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"
        )
        return route_completion(prompt, task="ask").strip()  # type: ignore[no-any-return]
    except Exception:
        return None  # any failure -> caller falls back to extractive


def ask(conn: sqlite3.Connection, question: str, k: int = 6) -> dict:
    """Main entry: question -> cited answer. Always returns; never raises on retrieval."""
    question = (question or "").strip()
    if not question:
        return {"question": question, "answer": "", "citations": [], "store": "sqlite",
                "model": "none", "note": "empty question"}

    hits = retrieve(conn, question, k=k)
    citations = [_citation(h) for h in hits]

    if not hits:
        return {"question": question, "answer": "",
                "answer_kind": "empty",
                "citations": [], "store": "sqlite", "model": "none",
                "note": "No matching chunks. Try broadening the question or ingest more docs."}

    llm = _llm_answer(question, hits)
    if llm:
        return {"question": question, "answer": llm, "answer_kind": "synthesized",
                "citations": citations, "store": "sqlite", "model": "router"}

    return {"question": question, "answer": _extractive_answer(question, hits),
            "answer_kind": "extractive",
            "citations": citations, "store": "sqlite", "model": "extractive"}
