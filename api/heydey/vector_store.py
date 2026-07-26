"""Vector store — ported from the proven BLOS `vector_store_v2.py` (S1, lock L7).

Public API frozen (upsert_points / store_chunks / search / search_by_vector /
keyword_search / delete_document / count_points / get_stats / list_documents);
same return shapes, same SQL, same BEGIN IMMEDIATE concurrency pattern.

Deliberate deltas from the source module (each forced by a heydey lock):
- connection seam: every function takes an explicit per-workspace connection from
  `heydey.workspaces.connect()` — one SQLite file per workspace is the isolation
  mechanism, so a module-global DB_PATH cannot exist here.
- apsw -> stdlib sqlite3 (this venv's Python loads extensions natively).
- store_chunks point ids: sha256-derived, NOT Python's salted hash() — hash() is
  per-process (PYTHONHASHSEED), so re-ingesting the same chunk in a new process
  minted a NEW id and duplicated the point. Deterministic ids make store_chunks
  idempotent across supervisor restarts.
"""

import hashlib
import json
import re
import sqlite3
import time

from .embedder import embed_texts


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def deterministic_point_id(chunk_id: str) -> int:
    """Stable 63-bit id from the chunk id (replaces salted hash())."""
    digest = hashlib.sha256(chunk_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> 1


def upsert_points(conn: sqlite3.Connection, points: list[tuple[str | int, list[float], dict]]) -> int:
    """Upsert (point_id, vector, payload) triples — the mirror/migration primitive.

    `BEGIN IMMEDIATE` + retry loop, verbatim from the proven module: a deferred
    transaction fails to UPGRADE to the write lock under contention and raises
    SQLITE_BUSY without honoring the busy-timeout (dropped writes, 2026-07-05).
    IMMEDIATE takes the write lock up front; the retry loop covers the rare
    full-timeout case. Idempotent upserts + ROLLBACK on error make retry safe.
    """
    max_retries = 6
    for attempt in range(max_retries):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = 0
                for point_id, vector, payload in points:
                    pid = str(point_id)
                    vec_json = json.dumps(vector)
                    pay_json = json.dumps(payload, ensure_ascii=False)
                    row = conn.execute("SELECT rowid FROM points WHERE point_id=?", (pid,)).fetchone()
                    if row:
                        rowid = row[0]
                        conn.execute(
                            "UPDATE points SET doc_id=?, source_file=?, created_at=?, text=?, payload=? WHERE rowid=?",
                            (payload.get("doc_id"), payload.get("source_file"), payload.get("created_at"),
                             payload.get("text", ""), pay_json, rowid),
                        )
                        conn.execute("DELETE FROM vec_points WHERE rowid=?", (rowid,))
                        conn.execute("INSERT INTO vec_points(rowid, embedding) VALUES (?, ?)", (rowid, vec_json))
                    else:
                        cursor = conn.execute(
                            "INSERT INTO points(point_id, doc_id, source_file, created_at, text, payload) VALUES (?,?,?,?,?,?)",
                            (pid, payload.get("doc_id"), payload.get("source_file"), payload.get("created_at"),
                             payload.get("text", ""), pay_json),
                        )
                        rowid = cursor.lastrowid
                        conn.execute("INSERT INTO vec_points(rowid, embedding) VALUES (?, ?)", (rowid, vec_json))
                    n += 1
                conn.execute("COMMIT")
                return n
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt == max_retries - 1:
                raise
            time.sleep(0.1 * (2 ** attempt))  # 0.1 -> 0.2 -> 0.4 -> 0.8 -> 1.6s
    return 0  # unreachable


def store_chunks(conn: sqlite3.Connection, chunks: list[dict], doc_id: str) -> int:
    """Standalone ingest path. Same payload shape as the proven module."""
    texts = [c["text"] for c in chunks]
    embeddings = embed_texts(texts)
    triples = []
    for chunk, embedding in zip(chunks, embeddings, strict=False):
        payload = {
            "doc_id": doc_id,
            "chunk_id": chunk["chunk_id"],
            "text": chunk["text"],
            "source_file": chunk["source_file"],
            "page_num": chunk.get("page_num"),
            "chunk_index": chunk["chunk_index"],
            "char_count": chunk.get("char_count", len(chunk["text"])),
            "created_at": chunk.get("created_at", ""),
        }
        # S2 slicer provenance (absent on legacy migrated points)
        if chunk.get("section") is not None:
            payload["section"] = chunk["section"]
        if chunk.get("title") is not None:
            payload["title"] = chunk["title"]
        if chunk.get("injection_risk") is not None:
            payload["injection_risk"] = chunk["injection_risk"]
        # S6b connector provenance (absent on doc/ops lanes): the Live Map counts
        # `connector_id`, the S6b gate/purity check keys on `source_type`.
        if chunk.get("source_type") is not None:
            payload["source_type"] = chunk["source_type"]
        if chunk.get("connector_id") is not None:
            payload["connector_id"] = chunk["connector_id"]
        triples.append((deterministic_point_id(chunk["chunk_id"]), embedding, payload))
    return upsert_points(conn, triples)


def search(conn: sqlite3.Connection, query: str, limit: int = 5, doc_id: str | None = None) -> list[dict]:
    """Semantic search — same return shape as the proven module."""
    query_embedding = embed_texts([query])[0]
    hits = search_by_vector(conn, query_embedding, limit=limit, doc_id=doc_id)
    return [
        {
            "score": round(h["score"], 3),
            "text": h["payload"]["text"],
            "source_file": h["payload"].get("source_file", ""),
            "page_num": h["payload"].get("page_num"),
            "doc_id": h["payload"].get("doc_id", ""),
        }
        for h in hits
    ]


def search_by_vector(conn: sqlite3.Connection, vector: list[float], limit: int = 5,
                     doc_id: str | None = None) -> list[dict]:
    """KNN by raw vector. Returns [{point_id, score, payload}]. score = cosine similarity."""
    # vec0 KNN can't join a WHERE on another table, so over-fetch when filtering.
    k = limit if doc_id is None else max(200, limit * 20)
    k = min(k, max(1, count_points(conn)))
    rows = conn.execute(
        "SELECT v.rowid, v.distance, p.point_id, p.payload, p.doc_id "
        "FROM vec_points v JOIN points p ON p.rowid = v.rowid "
        "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
        (json.dumps(vector), k),
    ).fetchall()
    out = []
    for _rowid, distance, point_id, pay_json, row_doc in rows:
        if doc_id is not None and row_doc != doc_id:
            continue
        out.append({"point_id": point_id, "score": 1.0 - distance, "payload": json.loads(pay_json)})
        if len(out) >= limit:
            break
    return out


def keyword_search(conn: sqlite3.Connection, query: str, limit: int = 5,
                   or_fallback: bool = False) -> list[dict]:
    """FTS5 keyword search (BM25-ranked). Raw FTS5 syntax first; on a parser trip
    (hyphens, quotes, ...), retry as a quoted phrase.

    or_fallback (S2): FTS5 space-joined terms are implicit AND — brittle for
    2-word ops queries (a "pricing posture" query excluded the pricing memo
    because the word "posture" never appears in it). When AND under-fills, the
    query re-runs disjunctively (OR-joined) and that list REPLACES the result:
    one consistent bm25 space, where all-term documents still rank first
    naturally. (Appending OR hits after AND hits was measured to bury a
    better-bm25 chunk at keyword rank 7 and cost the S2 gate.)
    """
    sql = ("SELECT p.point_id, p.payload, bm25(fts_points) FROM fts_points f "
           "JOIN points p ON p.rowid = f.rowid WHERE fts_points MATCH ? "
           "ORDER BY bm25(fts_points) LIMIT ?")
    try:
        rows = conn.execute(sql, (query, limit)).fetchall()
    except sqlite3.OperationalError:
        phrase = '"' + query.replace('"', '""') + '"'
        rows = conn.execute(sql, (phrase, limit)).fetchall()

    if or_fallback and len(rows) < limit:
        terms = re.findall(r"\w+", query)
        if len(terms) > 1:
            try:
                or_rows = conn.execute(sql, (" OR ".join(terms), limit)).fetchall()
                if len(or_rows) > len(rows):
                    rows = or_rows
            except sqlite3.OperationalError:
                pass
    return [{"point_id": pid, "payload": json.loads(pay), "bm25": rank} for pid, pay, rank in rows]


def delete_document(conn: sqlite3.Connection, doc_id: str) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        rowids = [r[0] for r in conn.execute("SELECT rowid FROM points WHERE doc_id=?", (doc_id,))]
        for rowid in rowids:
            conn.execute("DELETE FROM vec_points WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM points WHERE doc_id=?", (doc_id,))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return len(rowids)


def count_points(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM points").fetchone()[0]


def get_stats(conn: sqlite3.Connection) -> dict:
    try:
        return {"total_vectors": count_points(conn), "status": "green", "store": "sqlite"}
    except Exception:
        return {"total_vectors": 0, "status": "not_created", "store": "sqlite"}


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT doc_id, min(source_file), min(created_at) FROM points "
        "WHERE doc_id IS NOT NULL AND doc_id != '' GROUP BY doc_id"
    ).fetchall()
    return [{"doc_id": d, "source_file": s or "", "created_at": c or ""} for d, s, c in rows]
