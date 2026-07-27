"""The `llm` extractor lane of the graph rebuild — typed triples from a local model.

Why this file exists: the live graph had **0 rows in `relationships`** and 5,198
untyped co-retrieval edges. Deterministic extraction (gazetteer + regex, G1) recovers
identity and the obvious structure; a small local model is what turns prose like
"Dana decided ORION blocks the launch" into `ORION --BLOCKS--> launch`.

The diagnosis named **LLM hallucination as the likeliest failure of the whole
rebuild**, so this module is written as a filter, not as a generator:

- **Grounding (the load-bearing guard).** A triple whose subject or object does not
  literally appear in the source text is DROPPED. An 8B model will happily invent a
  plausible org name; a hallucinated node is worse than a missing one because it is
  uncitable, and cite-or-silent applies to the graph.
- **Whitelist, never coerce.** `predicate` / `subject_type` / `object_type` must be
  members of the locked vocabulary. An unknown predicate is DROPPED — it is never
  remapped onto a neighbouring one, because a wrong typed edge reads as a fact.
  The single normalisation applied is case-canonicalisation (`PREDICATES` are
  all-uppercase, `ENTITY_TYPES` all-lowercase), which by construction cannot map one
  vocabulary term onto a *different* one.
- **Fail closed and quiet.** Any exception — model unreachable, garbage JSON, wrong
  shape — returns `[]`. This runs inside ingest/backfill loops; it must never raise
  into them, and it must never fabricate to fill a gap.

Confidence is deliberately blunt: 0.8 when both endpoints recur in the document
(>=2 occurrences), 0.6 otherwise. A triple is only as well-grounded as its weaker
endpoint, so the *minimum* endpoint count decides. Nothing here writes at the old
0.55 floor that produced 3,319 junk nodes.

Vocabulary is imported lazily from :mod:`heydey.graph` (single source of truth) with
an identical local fallback, so this module is importable and testable on its own.
"""

from __future__ import annotations

import re

MODEL_DEFAULT = "qwen3:8b"
DEFAULT_TIMEOUT = 90.0
MAX_TEXT_CHARS = 6000  # one chunk is ~1-2k chars; this caps a pathological outlier
MAX_TRIPLES = 24
MIN_LABEL_CHARS = 2
MAX_LABEL_CHARS = 80
MAX_TOKENS = 768

CONF_RECURRING = 0.8
CONF_SINGLE = 0.6
RECUR_THRESHOLD = 2

EXTRACTOR = "llm"

# Locked vocabulary (contract). graph.py owns these once G1 lands; these copies keep
# graph_llm importable before that and MUST stay identical.
_FALLBACK_ENTITY_TYPES = frozenset({
    "person", "org", "product", "technology", "decision", "commitment",
    "money", "date", "place", "marker", "lock", "slice",
})
_FALLBACK_PREDICATES = frozenset({
    "MENTIONS", "AUTHORED_BY", "DECIDED_BY", "BLOCKS", "DEPENDS_ON", "OWNS",
    "WORKS_ON", "CLIENT_OF", "PRICED_AT", "DUE_ON", "SUPERSEDES", "PART_OF",
    "COMPETES_WITH", "CO_ACTIVITY",
})

SYSTEM = "You extract typed relationship triples from text. You reply with JSON only."

_WS = re.compile(r"\s+")


def vocabulary() -> tuple[frozenset[str], frozenset[str]]:
    """(entity_types, predicates) — from graph.py when available, else the local copy."""
    try:  # lazy: G1 owns graph.py and may land after this module
        from .graph import ENTITY_TYPES, PREDICATES

        return frozenset(ENTITY_TYPES), frozenset(PREDICATES)
    except Exception:
        return _FALLBACK_ENTITY_TYPES, _FALLBACK_PREDICATES


def _norm(value: str) -> str:
    """Casefold + collapse whitespace. Used on BOTH sides of the grounding check, so
    'Blue  Leaf\\nAI' in the text still matches 'Blue Leaf AI' from the model."""
    return _WS.sub(" ", value).strip().casefold()


def _prompt(text: str, types: frozenset[str], predicates: frozenset[str]) -> str:
    return (
        "Extract typed relationship triples that are EXPLICITLY stated in TEXT.\n\n"
        f"Allowed predicates (use EXACTLY one): {', '.join(sorted(predicates))}\n"
        f"Allowed entity types (use EXACTLY one): {', '.join(sorted(types))}\n\n"
        "Rules:\n"
        "1. subject and object MUST be copied VERBATIM from TEXT — never invent, "
        "expand, translate or rename them.\n"
        "2. State only what TEXT says. No world knowledge, no inference.\n"
        "3. If TEXT states no relationship, return {\"triples\": []}.\n"
        # 6, not 12: generation dominates the cost (~16 tok/s on this host, so a
        # 480-token reply is 30s). A/B over 6 real corpus chunks, 2026-07-27:
        # cap 12 -> 22.3s / 278 out-tokens / 18 kept; cap 6 -> 17.0s / 192 / 17 kept.
        # 24% cheaper per chunk for the same usable yield, over ~3,800 calls.
        "4. At most 6 triples, most important first.\n"
        "5. Reply with JSON ONLY. No prose, no markdown fences.\n\n"
        'Schema: {"triples": [{"subject": "...", "subject_type": "...", '
        '"predicate": "...", "object": "...", "object_type": "..."}]}\n\n'
        f'TEXT:\n"""\n{text}\n"""\n'
    )


def _complete(complete_fn, model: str, prompt: str, timeout: float) -> str:
    """Run the completion. `complete_fn` may return a Completion or a bare string."""
    fn = complete_fn
    if fn is None:
        from . import llm_client  # lazy: an injected complete_fn never touches the net

        fn = llm_client.complete
    result = fn(model, prompt, system=SYSTEM, max_tokens=MAX_TOKENS,
                temperature=0.0, timeout=timeout)
    return getattr(result, "text", result) or ""


def _rows(raw: str) -> list:
    """Parse the model's reply into a list of candidate rows. Accepts both
    {"triples": [...]} and a bare [...] (llm_client._loose_json unwraps the array
    out of the object, so both shapes arrive here)."""
    from .llm_client import parse_json

    data = parse_json(raw)
    if isinstance(data, dict):
        for key in ("triples", "edges", "relations"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return []
    return data if isinstance(data, list) else []


def _label(value) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().strip('"“”\'')


def extract_triples(text: str, *, model: str = MODEL_DEFAULT,
                    timeout: float = DEFAULT_TIMEOUT, complete_fn=None,
                    doc_text: str | None = None) -> list[dict]:
    """Typed triples for one chunk of text. Returns [] on ANY failure — never raises.

    Each item: {subject, subject_type, predicate, object, object_type, confidence}.

    `doc_text` is the recurrence window for the confidence rule ("recurs >=2x in the
    doc"): the model sees one chunk, but recurrence is a document-level property, so
    the backfill runner passes the whole document here. Defaults to `text`.
    Grounding is always checked against `text` — the words the model actually saw.
    """
    try:
        if not isinstance(text, str) or not text.strip():
            return []
        types, predicates = vocabulary()
        source = text[:MAX_TEXT_CHARS]
        raw = _complete(complete_fn, model, _prompt(source, types, predicates), timeout)
        rows = _rows(raw)

        grounding = _norm(source)
        recurrence = _norm(doc_text if isinstance(doc_text, str) and doc_text else text)

        out: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            subject, obj = _label(row.get("subject")), _label(row.get("object"))
            # case-canonicalisation ONLY — within a vocabulary that is uppercase for
            # predicates and lowercase for types this cannot turn one term into another
            predicate = _label(row.get("predicate")).upper().replace(" ", "_")
            s_type = _label(row.get("subject_type")).lower()
            o_type = _label(row.get("object_type")).lower()

            if predicate not in predicates or s_type not in types or o_type not in types:
                continue  # DROPPED, never remapped
            if not (MIN_LABEL_CHARS <= len(subject) <= MAX_LABEL_CHARS):
                continue
            if not (MIN_LABEL_CHARS <= len(obj) <= MAX_LABEL_CHARS):
                continue

            s_key, o_key = _norm(subject), _norm(obj)
            if s_key == o_key:
                continue  # self-loop: never a fact, always a parse artefact
            # THE guard: an entity the source text never mentions is a hallucination
            if s_key not in grounding or o_key not in grounding:
                continue
            if (s_key, predicate, o_key) in seen:
                continue
            seen.add((s_key, predicate, o_key))

            recurs = min(recurrence.count(s_key), recurrence.count(o_key))
            out.append({
                "subject": subject,
                "subject_type": s_type,
                "predicate": predicate,
                "object": obj,
                "object_type": o_type,
                "confidence": CONF_RECURRING if recurs >= RECUR_THRESHOLD else CONF_SINGLE,
            })
            if len(out) >= MAX_TRIPLES:
                break
        return out
    except Exception:
        return []  # fail closed and quiet: this runs inside ingest/backfill loops


def persist_triples(conn, triples: list[dict], *, workspace_id: str, doc_id: str,
                    chunk_id: str | None = None) -> tuple[int, int]:
    """Resolve both endpoints and write the typed edges. Returns (entities, edges).

    Every edge carries doc_id + chunk_id + extractor='llm' — cite-or-silent for the
    graph. Raises if G1's writer API is unavailable: the backfill runner catches it
    per document and records the doc `failed`, which is honest. Silently writing
    nothing and reporting `done` would not be.

    One exception is handled here rather than upstream: ``resolve_entity`` raises
    ValueError for a label it refuses to key (an empty canonical key, a money
    fragment under the floor). That is a verdict on ONE triple, not on the document,
    so the triple is dropped and the rest of the document still lands.
    """
    from .graph import add_edge
    from .graph_resolve import resolve_entity

    touched: set[int] = set()
    edges = 0
    for triple in triples:
        try:
            src = resolve_entity(conn, triple["subject"], triple["subject_type"], workspace_id,
                                 confidence=triple["confidence"], doc_id=doc_id, chunk_id=chunk_id)
            dst = resolve_entity(conn, triple["object"], triple["object_type"], workspace_id,
                                 confidence=triple["confidence"], doc_id=doc_id, chunk_id=chunk_id)
        except ValueError:
            continue  # the resolver refused this label — drop the triple, keep the doc
        touched.update((src, dst))
        if add_edge(conn, src, dst, triple["predicate"], confidence=triple["confidence"],
                    doc_id=doc_id, chunk_id=chunk_id, extractor=EXTRACTOR):
            edges += 1
    return len(touched), edges
