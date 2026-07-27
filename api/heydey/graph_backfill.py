"""Resumable graph backfill — the runner that fixes the 3.4% coverage failure.

`index_document` had only ever seen 43 of 1,271 documents: the migrated KB was copied
into `points` in place (never re-embedded, lock L7) and therefore never passed through
the extractor. This module walks the corpus **from the `points` table** — the text is
already there, so nothing is re-read from disk and nothing is ever re-embedded — and
runs one of two passes over it:

- ``deterministic`` — gazetteer + regex extraction (graph.index_document, G1). Cheap,
  no model, no network. Runs single-stream by construction: it is DB-bound and the
  workspace connection belongs to one thread.
- ``llm`` — typed triples via :mod:`heydey.graph_llm` on a few representative chunks
  per document.

**Cost, honestly — and the brief's number did not survive measurement.** The 4.72
s/chunk carried into this build is not reproducible for triple extraction. Measured on
this host 2026-07-27 against real corpus chunks (qwen3:8b via Ollama, warm, n=30):
**8.2-32.0 s/chunk, mean ~17 s**. Generation dominates — 200-480 output tokens at
~16 tok/s — so latency tracks how many triples the model emits, not chunk length.
That puts the full pass at 3 chunks x 1,271 docs = 3,813 calls ~= **18 h single-stream,
~6 h at --concurrency 3** (not 5 h / 1.7 h). ``--chunks-per-doc 1`` is the honest lever
if that is too long: 1,271 calls, ~6 h single-stream, ~2 h at 3 streams.
``--concurrency N`` runs N Ollama streams (2-3 is the useful range here) and the runner
prints a live ETA from the same constant. Model calls happen in worker threads;
**every DB read and write stays on the calling thread**, so the single per-workspace
sqlite connection is never touched cross-thread.

**Resumability.** Each document's outcome lands in `graph_backfill` (done | failed |
skipped, with counts and the error text) before the next document starts, so a kill at
hour 3 resumes at document N+1 rather than at 1. Known schema constraint, stated rather
than hidden: `graph_backfill.doc_id` is the PRIMARY KEY, so there is one row per
document *total*, not per (document, pass) — running the `llm` pass overwrites the
`deterministic` row for that document. Resumption is still correct per pass (it filters
on `pass`); what is lost is the older pass's bookkeeping, not its data.

**Per-document isolation.** One bad document logs, is recorded `failed`, and the run
continues. Nothing aborts a multi-hour run, and a failed document is simply re-tried on
the next invocation (edge writes are idempotent through the UNIQUE constraint).

    python -m heydey.graph_backfill --workspace blueleaf --pass deterministic
    python -m heydey.graph_backfill --workspace blueleaf --pass llm --concurrency 3
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone

from . import graph_llm

PASSES = ("deterministic", "llm")
DEFAULT_CHUNKS_PER_DOC = 3
DEFAULT_CHUNK_TIMEOUT = 90.0
MIN_CHUNK_CHARS = 120  # below this a chunk is a heading/footer, not evidence
# Measured 2026-07-27 on this host (qwen3:8b via Ollama, warm, n=30 REAL corpus chunks
# of 214-3,531 chars): 8.2-32.0 s, mean ~17 s. The 4.72 s/chunk in the build brief was
# measured on something other than this prompt and does not reproduce — the ETA printed
# to the operator has to be the number we actually saw.
MEASURED_S_PER_CHUNK = 17.0

# Proper-noun-ish spans — the density signal for representative-chunk selection.
_CANDIDATE = re.compile(r"\b(?:[A-Z]{2,}|[A-Z][\w'’-]+)\b")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


# ── corpus reads (points table only — never disk, never re-embed) ─────────────
def list_doc_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT doc_id FROM points "
        "WHERE doc_id IS NOT NULL AND doc_id != '' ORDER BY doc_id"
    ).fetchall()
    return [r[0] for r in rows]


def load_chunks(conn: sqlite3.Connection, doc_id: str) -> list[dict]:
    """[{chunk_id, text}] in document order. chunk_id comes from the point payload
    (the retrieval breadcrumb the receipts already cite); point_id is the fallback."""
    rows = conn.execute(
        "SELECT point_id, text, payload FROM points WHERE doc_id=? ORDER BY rowid",
        (doc_id,),
    ).fetchall()
    chunks = []
    for point_id, text, payload in rows:
        if not text:
            continue
        chunk_id = point_id
        try:
            chunk_id = (json.loads(payload) or {}).get("chunk_id") or point_id
        except (TypeError, ValueError):
            pass
        chunks.append({"chunk_id": str(chunk_id), "text": text})
    return chunks


def select_chunks(chunks: list[dict], k: int) -> list[dict]:
    """The ~k most representative chunks of a document, in document order.

    Ranking (documented so the sample is auditable, not "the model's call"):
      1. substantial chunks first — >= MIN_CHUNK_CHARS, so headers/footers lose;
      2. then entity density — count of DISTINCT proper-noun-ish spans, because a
         relationship needs two named endpoints and density is where they live;
      3. then length, as the tie-break.
    Density beats raw length deliberately: the longest chunk in this corpus is often a
    log dump or a table of contents, which yields nothing but noise.
    """
    if k <= 0 or not chunks:
        return []
    ranked = sorted(
        enumerate(chunks),
        key=lambda item: (
            len(item[1]["text"]) >= MIN_CHUNK_CHARS,
            len(set(_CANDIDATE.findall(item[1]["text"]))),
            len(item[1]["text"]),
        ),
        reverse=True,
    )
    return [chunk for _, chunk in sorted(ranked[:k], key=lambda item: item[0])]


# ── ledger ───────────────────────────────────────────────────────────────────
def resolved_doc_ids(conn: sqlite3.Connection, pass_name: str) -> set[str]:
    """Documents this pass has already settled — `done` or `skipped` (no text to
    process). Both are terminal: a document only becomes processable again through a
    re-ingest, which the caller drives explicitly with --doc/doc_ids."""
    rows = conn.execute(
        "SELECT doc_id FROM graph_backfill WHERE pass=? AND status IN ('done','skipped')",
        (pass_name,),
    ).fetchall()
    return {r[0] for r in rows}


def _record(conn: sqlite3.Connection, doc_id: str, pass_name: str, status: str,
            entities: int, edges: int, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO graph_backfill(doc_id, pass, status, entities, edges, error, updated_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET "
        "pass=excluded.pass, status=excluded.status, entities=excluded.entities, "
        "edges=excluded.edges, error=excluded.error, updated_at=excluded.updated_at",
        (doc_id, pass_name, status, entities, edges,
         str(error)[:500] if error else None, _now()),
    )


def _edge_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]


# ── the two passes ───────────────────────────────────────────────────────────
def _extract_doc(chunks: list[dict], *, model: str, timeout: float, complete_fn,
                 doc_timeout: float | None) -> list[tuple[str, list[dict]]]:
    """Pure model work for one document — NO database access (may run off-thread).

    The per-document deadline is hard but cooperative: it is checked between chunks,
    while the per-chunk socket timeout bounds any single call. Blowing the deadline
    raises, which the caller records as `failed` — the document is re-tried next run.
    """
    deadline = (time.monotonic() + doc_timeout) if doc_timeout is not None else None
    doc_text = "\n".join(c["text"] for c in chunks)
    out = []
    for index, chunk in enumerate(chunks):
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(f"per-doc deadline exceeded after {index}/{len(chunks)} chunks")
        out.append((chunk["chunk_id"], graph_llm.extract_triples(
            chunk["text"], model=model, timeout=timeout,
            complete_fn=complete_fn, doc_text=doc_text)))
    return out


def _run_deterministic(conn: sqlite3.Connection, doc_id: str,
                       workspace_id: str) -> tuple[int, int, int, str]:
    """gazetteer/regex pass for one document. Returns (entities, edges, chunks, status)."""
    from . import graph  # lazy: G1 owns graph.py

    chunks = load_chunks(conn, doc_id)
    text = "\n\n".join(c["text"] for c in chunks)
    if not text.strip():
        return 0, 0, 0, "skipped"
    before = _edge_count(conn)
    entities = graph.index_document(conn, doc_id, text, workspace_id)
    return int(entities or 0), _edge_count(conn) - before, len(chunks), "done"


# ── extraction stream (concurrency lives here; DB access does not) ────────────
def _stream(conn, doc_ids, *, pass_name, chunks_per_doc, model, timeout, complete_fn,
            doc_timeout, concurrency):
    """Yield (doc_id, extracted, exc) in submission order.

    For the deterministic pass nothing is pre-computed — the work is DB-bound and
    happens on the calling thread at persist time.
    """
    if pass_name != "llm":
        for doc_id in doc_ids:
            yield doc_id, None, None
        return

    def job(chunks):
        return _extract_doc(chunks, model=model, timeout=timeout,
                            complete_fn=complete_fn, doc_timeout=doc_timeout)

    if concurrency <= 1:
        for doc_id in doc_ids:
            try:
                yield doc_id, job(select_chunks(load_chunks(conn, doc_id), chunks_per_doc)), None
            except Exception as exc:  # KeyboardInterrupt deliberately propagates
                yield doc_id, None, exc
        return

    pending: deque = deque()
    remaining = iter(doc_ids)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        def fill():
            # keep every stream fed, one document of slack; chunk loading is a DB read
            # so it stays here, on the calling thread.
            while len(pending) < concurrency + 1:
                doc_id = next(remaining, None)
                if doc_id is None:
                    return
                try:
                    chunks = select_chunks(load_chunks(conn, doc_id), chunks_per_doc)
                except Exception as exc:
                    future: Future = Future()
                    future.set_exception(exc)
                else:
                    future = pool.submit(job, chunks)
                pending.append((doc_id, future))

        fill()
        while pending:
            doc_id, future = pending.popleft()
            try:
                yield doc_id, future.result(), None
            except Exception as exc:
                yield doc_id, None, exc
            fill()


# ── the runner ───────────────────────────────────────────────────────────────
def backfill(conn: sqlite3.Connection, *, workspace_id: str, pass_name: str,
             doc_ids: list[str] | None = None,
             chunks_per_doc: int = DEFAULT_CHUNKS_PER_DOC,
             limit: int | None = None, progress=print,
             model: str = graph_llm.MODEL_DEFAULT, concurrency: int = 1,
             timeout: float = DEFAULT_CHUNK_TIMEOUT, doc_timeout: float | None = None,
             complete_fn=None, persist_fn=None) -> dict:
    """Run one pass over the corpus, resumably. Returns a summary dict.

    `complete_fn` (injected into graph_llm) and `persist_fn` (defaults to
    graph_llm.persist_triples) are the seams: tests drive the whole runner with no
    network and no dependency on G1's writers.
    """
    if pass_name not in PASSES:
        raise ValueError(f"pass_name must be one of {PASSES}, got {pass_name!r}")
    persist = persist_fn or graph_llm.persist_triples
    if doc_timeout is None:
        doc_timeout = max(1, chunks_per_doc) * timeout

    all_ids = list(doc_ids) if doc_ids is not None else list_doc_ids(conn)
    already = resolved_doc_ids(conn, pass_name)
    todo = [d for d in all_ids if d not in already]
    resumed = len(all_ids) - len(todo)
    if limit is not None:
        todo = todo[:limit]

    progress(f"backfill pass={pass_name} workspace={workspace_id} docs={len(all_ids)} "
             f"todo={len(todo)} already_done={resumed} chunks/doc={chunks_per_doc} "
             f"concurrency={concurrency}")
    if pass_name == "llm" and todo:
        calls = len(todo) * chunks_per_doc
        estimate = calls * MEASURED_S_PER_CHUNK / max(1, concurrency)
        progress(f"estimate: {calls} chunks x {MEASURED_S_PER_CHUNK}s measured / "
                 f"{max(1, concurrency)} stream(s) = ~{_hms(estimate)}")

    stats = {"done": 0, "failed": 0, "skipped": 0, "entities": 0, "edges": 0, "chunks": 0}
    started = time.monotonic()
    processed = 0

    for doc_id, extracted, exc in _stream(
        conn, todo, pass_name=pass_name, chunks_per_doc=chunks_per_doc, model=model,
        timeout=timeout, complete_fn=complete_fn, doc_timeout=doc_timeout,
        concurrency=concurrency,
    ):
        processed += 1
        doc_started = time.monotonic()
        entities = edges = n_chunks = 0
        error = None
        try:
            if exc is not None:
                raise exc
            if pass_name == "llm":
                n_chunks = len(extracted or [])
                for chunk_id, triples in extracted or []:
                    if not triples:
                        continue
                    got_entities, got_edges = persist(
                        conn, triples, workspace_id=workspace_id,
                        doc_id=doc_id, chunk_id=chunk_id)
                    entities += got_entities
                    edges += got_edges
                status = "done" if n_chunks else "skipped"
            else:
                entities, edges, n_chunks, status = _run_deterministic(
                    conn, doc_id, workspace_id)
        except Exception as failure:  # per-doc isolation: log, record, keep going
            status, error = "failed", f"{type(failure).__name__}: {failure}"
            entities = edges = 0

        _record(conn, doc_id, pass_name, status, entities, edges, error)
        stats[status] += 1
        stats["entities"] += entities
        stats["edges"] += edges
        stats["chunks"] += n_chunks

        elapsed = time.monotonic() - started
        eta = (len(todo) - processed) * (elapsed / max(1, processed))
        progress(f"[{processed}/{len(todo)}] {doc_id} {status} entities={entities} "
                 f"edges={edges} chunks={n_chunks} {time.monotonic() - doc_started:.1f}s "
                 f"eta={_hms(eta)}" + (f" error={error}" if error else ""))

    return {
        "pass": pass_name,
        "workspace": workspace_id,
        "docs_total": len(all_ids),
        "already_done": resumed,
        "processed": processed,
        "elapsed_s": round(time.monotonic() - started, 1),
        **stats,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m heydey.graph_backfill",
        description="Resumable graph backfill over a workspace's stored chunks.")
    parser.add_argument("--workspace", default="blueleaf")
    parser.add_argument("--pass", dest="pass_name", choices=PASSES, required=True)
    parser.add_argument("--limit", type=int, default=None, help="stop after N documents")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="parallel Ollama streams (llm pass only; 2-3 is useful here)")
    parser.add_argument("--chunks-per-doc", type=int, default=DEFAULT_CHUNKS_PER_DOC)
    parser.add_argument("--model", default=graph_llm.MODEL_DEFAULT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_CHUNK_TIMEOUT,
                        help="per-chunk model timeout (seconds)")
    parser.add_argument("--doc", action="append", dest="docs", default=None,
                        help="restrict to this doc_id (repeatable)")
    parser.add_argument("--reset", action="store_true",
                        help="clear this pass's ledger rows first (full re-run)")
    args = parser.parse_args(argv)

    from . import workspaces

    conn = workspaces.connect(args.workspace)
    try:
        if args.reset:
            conn.execute("DELETE FROM graph_backfill WHERE pass=?", (args.pass_name,))
        report = backfill(
            conn, workspace_id=args.workspace, pass_name=args.pass_name,
            doc_ids=args.docs, chunks_per_doc=args.chunks_per_doc, limit=args.limit,
            model=args.model, concurrency=args.concurrency, timeout=args.timeout,
            progress=lambda line: print(line, flush=True),  # detached runs must stream
        )
    finally:
        conn.close()
    print(json.dumps(report, indent=2))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
