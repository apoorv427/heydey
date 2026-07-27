"""Graph extraction engine (Executor Contract B) — the anti-LightRAG contract.

A living entity/relationship graph that is a **byproduct of normal use**, never a
scheduled job — so it cannot silently die the way LightRAG did. Pure SQLite,
event-driven, no model call anywhere on this path.

G1 rebuild. Five measured failures of the v1 graph, and what replaced each:

1. **coverage 3.4%** (43 of 1,271 docs ever indexed) — :func:`index_document` is
   now cheap, per-doc isolated and re-runnable, so a full backfill is safe.
2. **identity** (26 nodes for a single product, keyed on ``(label, type, source_doc_id)``) —
   every write goes through :func:`heydey.graph_resolve.resolve_entity` FIRST,
   keyed on ``(canonical_key, type)``. One node per real-world thing.
3. **precision ~18%** (3,319 entities parked at the 0.55 floor; "How",
   "Desktop", "Downloads", "Jul" were nodes) — proper-noun candidates are GATED
   (recurrence >= 2 in the doc, or a heading line, or a gazetteer hit) and run
   through a real stopword wall (function words, months, weekdays, OS folders).
4. **zero relationships** (the v1 ``relationships`` table had 0 rows) — typed
   edges are first-class rows in ``graph_edges`` via :func:`add_edge`, and every
   edge is refused unless it carries provenance (``doc_id``). Cite-or-silent
   applies to the graph: an edge nobody can source is not shippable.
5. **hub bias** (unit-less money fragments and markers ranked top) — same-doc
   co-mention edges are scored by **PMI**, not raw count, so a token that
   co-occurs with everything scores ~0 and falls out of the panel.

Compatibility: the v1 tables (``entities`` / ``activity_edges``) are still
maintained as a read-only-shaped MIRROR so the pre-rebuild readers (the S4c
gate, Foundry's corpus scan, the Sentinel stall canary) keep working while the
backfill lands. ``graph_*`` is the source of truth; the mirror is written last
and its row ids live in a disjoint high range (:data:`LEGACY_ID_BASE`) so the
two id spaces can never be confused.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import graph_resolve
# resolve_entity is re-exported: callers that write entities outside the ingest
# path (a future LLM pass, the entity API) must go through the same front door.
from .graph_resolve import ENTITY_TYPES, canonical_key, resolve_entity  # noqa: F401

# Typed, directional relations (fixed vocabulary — shared contract).
PREDICATES = frozenset({
    "MENTIONS", "AUTHORED_BY", "DECIDED_BY", "BLOCKS", "DEPENDS_ON", "OWNS",
    "WORKS_ON", "CLIENT_OF", "PRICED_AT", "DUE_ON", "SUPERSEDES", "PART_OF",
    "COMPETES_WITH", "CO_ACTIVITY",
})
EXTRACTORS = frozenset({"gazetteer", "regex", "llm", "pmi", "co_activity"})

# Curated project / product vocabulary — the entities ops questions actually
# turn on. Installation-specific names are MACHINE-LOCAL state (corpus.json
# `graph_seed`, merged over a minimal default) — never repo literals. Tests and
# embedders may set the module attribute directly.
DEFAULT_PROJECTS = ["Heydey"]
KNOWN_PROJECTS: list[str] | None = None  # None -> corpus.json graph_seed + defaults

# Per-document co-mention fan-out cap: 10 entities -> 45 pairs, so a long doc
# cannot explode the edge table. MAX_ENTITIES_PER_DOC bounds the mention rows
# too: one merged log in the live corpus yielded 1,150 candidates on its own.
MAX_COMENTION_ENTITIES = 10
MAX_ENTITIES_PER_DOC = 200

# RENDER-TIME prior only — the data is untouched, this reorders the panel.
# PMI kills the co-occurrence hubs; it does not know that a date or a marker is
# scaffolding rather than an actor. Measured on the live corpus: without this
# the top 12 nodes were 7 dates. Everything stays in the graph and stays
# reachable through entity_profile/neighbors.
PANEL_TYPE_PRIOR = {"date": 0.25, "marker": 0.25, "lock": 0.5, "slice": 0.5}

# v1 mirror ids start here so a v1 id can never collide with a graph_entities id.
LEGACY_ID_BASE = 1_000_000_000

_ISO = "%Y-%m-%dT%H:%M:%S"


def _project_names() -> list[str]:
    names = KNOWN_PROJECTS
    if names is None:
        from . import config
        names = [*config.load_corpus_config().get("graph_seed", []), *DEFAULT_PROJECTS]
    return list(dict.fromkeys(names)) or DEFAULT_PROJECTS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── deterministic extraction (pass 0/1) ──────────────────────────────────────

_MARKER_RE = re.compile(
    r"\b(PENDING|GATE|BLOCKER|BLOCKED|LOCKED|LOCK|TODO|DECISION|GREEN|RED)\b")
_LOCK_RE = re.compile(r"\bL\d{1,3}\b")
_SLICE_RE = re.compile(r"\bS\d[a-c]?\b")
_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
_DATE_RE = re.compile(
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b"
    rf"|\b(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})[a-z]*\.?,?\s+\d{{4}}\b")

# A capitalised run of 1-3 words, possessive included so identity can strip it.
_CANDIDATE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.\-]*(?:['’]s)?(?:\s+[A-Z][A-Za-z0-9&.\-]*(?:['’]s)?){0,2})\b")
_HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s+|\*\*.+\*\*\s*$|[-*]\s+\*\*)")

# Words that dropped out of a multi-word candidate's edges ("The Foundry" ->
# "Foundry"). Deliberately small: only true sentence/determiner starters.
_EDGE_STOP = {
    "a", "an", "and", "at", "but", "by", "for", "from", "if", "in", "into", "it",
    "of", "on", "or", "our", "so", "that", "the", "their", "then", "there",
    "these", "this", "those", "to", "we", "with",
}

# The stopword wall. The v1 list had 22 entries and let "How", "Desktop",
# "Downloads", "Jul", "Documents", "Open", "Two", "Why" through — 307 pure
# function words became nodes. Everything below is a measured junk source.
_STOPWORDS = {
    # function / sentence words
    "a", "about", "above", "after", "again", "against", "all", "also", "always",
    "am", "an", "and", "another", "any", "anyone", "anything", "are", "around",
    "as", "at", "back", "be", "because", "been", "before", "being", "below",
    "best", "between", "both", "but", "by", "came", "can", "cannot", "come",
    "could", "did", "do", "does", "doing", "done", "down", "during", "each",
    "either", "else", "enough", "even", "ever", "every", "everyone",
    "everything", "few", "first", "for", "found", "from", "further", "get",
    "gets", "getting", "give", "go", "going", "got", "had", "has", "have",
    "having", "he", "help", "her", "here", "hers", "herself", "him", "himself",
    "his", "how", "however", "i", "if", "in", "instead", "into", "is", "it",
    "its", "itself", "just", "keep", "last", "later", "least", "less", "let",
    "like", "made", "make", "makes", "making", "many", "may", "maybe", "me",
    "might", "mine", "more", "most", "much", "must", "my", "myself", "need",
    "needs", "never", "new", "next", "no", "none", "nor", "not", "note",
    "notes", "nothing", "now", "of", "off", "often", "on", "once", "one",
    "only", "onto", "open", "or", "other", "others", "our", "ours", "out",
    "over", "own", "per", "please", "put", "rather", "read", "really", "run",
    "running", "said", "same", "say", "says", "second", "see", "seen", "set",
    "shall", "she", "should", "since", "so", "some", "someone", "something",
    "still", "such", "sure", "take", "taken", "than", "that", "the", "their",
    "theirs", "them", "themselves", "then", "there", "these", "they", "thing",
    "things", "third", "this", "those", "though", "three", "through", "thus",
    "to", "together", "too", "two", "under", "until", "up", "upon", "us",
    "use", "used", "uses", "using", "very", "want", "was", "way", "we", "well",
    "went", "were", "what", "when", "where", "whether", "which", "while", "who",
    "whom", "whose", "why", "will", "with", "within", "without", "would", "yes",
    "yet", "you", "your", "yours", "build", "built", "add", "added", "close",
    "closed", "start", "started", "stop", "stopped", "check", "checked",
    # months + weekdays (v1 shipped "Jul" as a node)
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri",
    "sat", "sun", "today", "tomorrow", "yesterday", "week", "month", "year",
    # OS / filesystem noise (v1 shipped "Desktop", "Downloads", "Documents")
    "desktop", "downloads", "documents", "library", "applications", "users",
    "volumes", "system", "movies", "music", "pictures", "public", "private",
    "trash", "finder", "folder", "folders", "file", "files", "path", "home",
    "icloud", "dropbox", "onedrive", "drive", "screenshot", "screenshots",
    "untitled", "temp", "tmp", "backup", "archive", "archives",
    # document-structure noise
    "status", "owner", "owners", "phase", "section", "sections", "summary",
    "overview", "appendix", "table", "tables", "figure", "page", "pages",
    "title", "draft", "final", "version", "changelog", "readme", "index",
    "main", "general", "misc", "other", "todo", "update", "updates", "context",
    "personal", "example", "examples", "notes", "report", "reports",
    "part", "parts", "full", "item", "items", "step", "steps", "line", "lines",
    "point", "points", "case", "cases", "level", "levels", "list", "lists",
}


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    """Blank out already-typed spans (preserving offsets) so the proper-noun
    pass cannot re-mint a lock/slice/date/money/gazetteer hit as an org."""
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _typed_pass(text: str) -> tuple[list[tuple[str, str, float]], list[tuple[int, int]]]:
    """Gazetteer + typed regex. Returns [(surface, type, confidence)] and the
    consumed spans."""
    found: list[tuple[str, str, float]] = []
    spans: list[tuple[int, int]] = []

    names = _project_names()
    if names:
        gazetteer = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
        for match in gazetteer.finditer(text):
            found.append((match.group(1), "product", 0.95))
            spans.append(match.span())

    for pattern, etype, conf in (
        (_DATE_RE, "date", 0.9),
        (_LOCK_RE, "lock", 0.95),
        (_SLICE_RE, "slice", 0.95),
        (_MARKER_RE, "marker", 0.9),
    ):
        for match in pattern.finditer(text):
            found.append((match.group(0), etype, conf))
            spans.append(match.span())

    for match in graph_resolve.MONEY_RE.finditer(text):
        parsed = graph_resolve.parse_money(match.group(0))
        if parsed is None:
            continue
        # A unit-less fragment under the floor stays a CANDIDATE (so callers can
        # see what was seen) but sits below the node gate and never mints a node.
        found.append((match.group(0).strip(), "money", 0.95 if parsed["qualifies"] else 0.4))
        spans.append(match.span())

    return found, spans


def _trim_edges(label: str) -> str:
    """Drop determiner/preposition words from the ends of a multi-word run."""
    words = label.split()
    while words and canonical_key(words[0]) in _EDGE_STOP:
        words.pop(0)
    while words and canonical_key(words[-1]) in _EDGE_STOP:
        words.pop()
    return " ".join(words)


def _is_junk(key: str) -> bool:
    if len(key) < 3 or key.isdigit():
        return True
    tokens = key.split()
    if not tokens:
        return True
    if all(token in _STOPWORDS for token in tokens):
        return True
    if len(tokens) == 1 and tokens[0] in _STOPWORDS:
        return True
    return False


_LOWER_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9'’\-]+\b")


def _candidate_pass(masked: str) -> list[tuple[str, str, float]]:
    """GATED proper-noun candidates. A capitalised run qualifies only if it
    recurs >= 2x in the document or appears on a heading line — the gate that
    deletes the 3,319-row 0.55-confidence junk class."""
    # Sentence-start artefacts: "Full", "Part", "Live", "Next" are capitalised
    # only because a sentence began with them. Structural test, no word list —
    # if a single word occurs lowercase MORE often than capitalised in this
    # document, it is a common noun, not a name. Measured on the live corpus:
    # this drops Full/Part/Live/Agent while keeping Python/OpenAI/Claude, which
    # a plain "appears lowercase anywhere" test threw away. ALLCAPS acronyms
    # (API, CEO, MCP) are exempt — they have no lowercase-in-prose form.
    lowercase_counts = Counter(_LOWER_TOKEN_RE.findall(masked))

    counts: Counter = Counter()
    surfaces: dict[str, list[str]] = defaultdict(list)
    headings: set[str] = set()
    allcaps: set[str] = set()

    for line in masked.splitlines():
        is_heading = bool(_HEADING_RE.match(line)) or (
            line.strip().endswith(":") and 0 < len(line.strip()) <= 60)
        for match in _CANDIDATE_RE.finditer(line):
            surface = _trim_edges(match.group(1))
            if not surface:
                continue
            key = canonical_key(surface)
            if _is_junk(key):
                continue
            counts[key] += 1
            if surface not in surfaces[key]:
                surfaces[key].append(surface)
            if is_heading:
                headings.add(key)
            if surface.isupper():
                allcaps.add(key)

    out: list[tuple[str, str, float]] = []
    for key, count in counts.items():
        recurring = count >= 2
        if not recurring and key not in headings:
            continue
        if " " not in key and key not in allcaps and lowercase_counts[key] > count:
            continue
        multiword = " " in key
        if recurring and multiword:
            conf = 0.85
        elif recurring:
            conf = 0.75
        else:
            conf = 0.7
        # every distinct surface form is emitted so aliases stay complete
        for surface in surfaces[key][:4]:
            out.append((surface, "org", conf))
    return out


def extract_entities(text: str) -> list[dict]:
    """Deterministic extraction. ``[{label, type, surface, confidence}]``, deduped.

    No model call: gazetteer, then typed regex (date/lock/slice/marker/money),
    then GATED proper-noun candidates over the text those passes did not consume.

    ``label`` is the canonical display form; ``surface`` is the spelling actually
    seen in the document (kept so every alias reaches ``graph_aliases`` — the
    possessive form is a different alias of the same entity, not a new node).
    Money is the one exception: it is normalised to its canonical spelling
    *before* identity forms, so every spelling of one amount is one row here.
    """
    typed, spans = _typed_pass(text)
    candidates = _candidate_pass(_mask(text, spans))

    found: dict[tuple[str, str, str], float] = {}
    for raw, etype, conf in (*typed, *candidates):
        if etype not in ENTITY_TYPES:
            continue
        if etype == "money":
            parsed = graph_resolve.parse_money(raw)
            if parsed is None:
                continue
            label = surface = parsed["label"]
        else:
            label, surface = graph_resolve.display_label(raw), str(raw).strip()
        if not (1 < len(label) <= 60):
            continue
        key = (label, etype, surface)
        found[key] = max(found.get(key, 0.0), conf)

    return [{"label": label, "type": etype, "surface": surface,
             "confidence": round(conf, 2)}
            for (label, etype, surface), conf in found.items()]


# ── typed edges ──────────────────────────────────────────────────────────────

def add_edge(conn: sqlite3.Connection, src_id: int, dst_id: int, predicate: str,
             *, confidence: float = 0.5, weight: float = 1.0,
             doc_id: str | None = None, chunk_id: str | None = None,
             extractor: str | None = None) -> bool:
    """Assert one typed, sourced edge. True if a new row was created.

    Fail-closed on three things, because a graph you cannot audit is worse than
    no graph: an unknown predicate, an unknown extractor, and a missing
    ``doc_id`` all raise :class:`ValueError`. Re-asserting the same
    (src, dst, predicate, doc) bumps weight instead of duplicating the row.
    """
    if predicate not in PREDICATES:
        raise ValueError(f"unknown predicate {predicate!r}; allowed: {sorted(PREDICATES)}")
    if extractor not in EXTRACTORS:
        raise ValueError(f"unknown extractor {extractor!r}; allowed: {sorted(EXTRACTORS)}")
    if not doc_id:
        raise ValueError("edge refused: no provenance (doc_id) — cite-or-silent")
    if src_id == dst_id:
        return False

    now = _now()
    row = conn.execute(
        "SELECT id, weight FROM graph_edges"
        " WHERE src_id=? AND dst_id=? AND predicate=? AND doc_id=?",
        (src_id, dst_id, predicate, doc_id),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE graph_edges SET weight = ?, confidence = MAX(COALESCE(confidence,0), ?),"
            " asserted_at = ? WHERE id = ?",
            (float(row[1] or 0.0) + float(weight), confidence, now, row[0]),
        )
        return False
    conn.execute(
        "INSERT INTO graph_edges(src_id, dst_id, predicate, confidence, weight, doc_id,"
        " chunk_id, extractor, asserted_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (src_id, dst_id, predicate, confidence, weight, doc_id, chunk_id or "",
         extractor, now),
    )
    return True


# ── ingest ───────────────────────────────────────────────────────────────────

def _mirror_v1(conn: sqlite3.Connection, doc_id: str, workspace_id: str,
               ents: list[dict]) -> None:
    """Maintain the v1 ``entities`` mirror with IDENTITY-PRESERVING sync.

    (S4c regression: delete+reinsert minted new rowids on every re-ingest and
    orphaned EVERY activity_edge — 1,344 dead edges found live. An entity that
    survives a re-ingest keeps its id, so edge history stays attached.)
    """
    existing = {(r[1], r[2]): r[0] for r in conn.execute(
        "SELECT id, label, type FROM entities WHERE source_doc_id=?", (doc_id,))}
    fresh = {(e["label"], e["type"]): e for e in ents}

    gone_ids = [existing[key] for key in existing.keys() - fresh.keys()]
    if gone_ids:
        marks = ",".join("?" * len(gone_ids))
        conn.execute(f"DELETE FROM entities WHERE id IN ({marks})", gone_ids)
        conn.execute(
            f"DELETE FROM activity_edges WHERE entity_a IN ({marks})"
            f" OR entity_b IN ({marks})", (*gone_ids, *gone_ids))

    for key in existing.keys() & fresh.keys():
        conn.execute("UPDATE entities SET confidence=? WHERE id=?",
                     (fresh[key]["confidence"], existing[key]))

    new_keys = [key for key in fresh if key not in existing]
    if not new_keys:
        return
    high = conn.execute("SELECT COALESCE(MAX(id), 0) FROM entities").fetchone()[0]
    next_id = max(int(high) + 1, LEGACY_ID_BASE)
    now = _now()
    conn.executemany(
        "INSERT INTO entities(id, label, type, workspace_id, source_doc_id, confidence,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        [(next_id + offset, key[0], key[1], workspace_id, doc_id,
          fresh[key]["confidence"], now)
         for offset, key in enumerate(new_keys)],
    )


def index_document(conn: sqlite3.Connection, doc_id: str, text: str,
                   workspace_id: str, *, min_conf: float = 0.55) -> int:
    """Extract + persist a doc's entities and its co-mention edges. Idempotent.

    Returns the number of distinct entities this doc resolved to. Per-document
    error isolation is absolute: any failure rolls the whole doc back and
    returns 0 — one bad doc can never abort a run or corrupt the graph.
    The per-doc ``graph_backfill`` ledger is deliberately NOT written here: it
    is owned by :mod:`heydey.graph_backfill`, whose resumption filters on the
    ``pass`` column, and a write from the ingest path would clobber the llm
    pass's row for that document.
    """
    try:
        ents = sorted((e for e in extract_entities(text) if e["confidence"] >= min_conf),
                      key=lambda e: (-e["confidence"], e["label"]))[:MAX_ENTITIES_PER_DOC]
    except Exception:
        return 0

    # BEGIN must be INSIDE the guard: a busy/locked db (a concurrent backfill
    # stream, or a caller that already inside a transaction) raised straight out
    # of here and broke the "one bad doc returns 0" contract. One attempt only —
    # sqlite's own busy_timeout already does the waiting, so an application-level
    # retry loop just multiplies that wait (measured: 6 retries turned a locked
    # write into a 195s stall). Give up quietly; the backfill ledger records the
    # doc as failed and a resume run picks it up.
    try:
        conn.execute("BEGIN IMMEDIATE")
    except Exception:
        return 0

    try:
        graph_resolve.forget_document(conn, doc_id)
        resolved: list[tuple[int, float]] = []
        seen: set[int] = set()
        for ent in ents:
            entity_id = graph_resolve.resolve_entity(
                conn, ent.get("surface") or ent["label"], ent["type"], workspace_id,
                confidence=ent["confidence"], doc_id=doc_id)
            if entity_id not in seen:
                seen.add(entity_id)
                resolved.append((entity_id, ent["confidence"]))

        # structural inference, no model call: same-document co-mention edges.
        # Weight starts at 1.0 and is re-scored to PMI by mine_relationships(),
        # which is what kills hub bias.
        top = [eid for eid, _ in sorted(resolved, key=lambda p: -p[1])][:MAX_COMENTION_ENTITIES]
        for i, src in enumerate(top):
            for dst in top[i + 1:]:
                a, b = (src, dst) if src < dst else (dst, src)
                add_edge(conn, a, b, "CO_ACTIVITY", confidence=0.3, weight=1.0,
                         doc_id=doc_id, extractor="pmi")

        _mirror_v1(conn, doc_id, workspace_id, ents)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return 0
    return len(seen)


def mine_relationships(conn: sqlite3.Connection) -> int:
    """Re-score every co-mention edge with **PMI** over the whole corpus.

    ``pmi(a,b) = log2( n_ab * N / (n_a * n_b) )`` in documents. A hub token that
    appears with everything gets pmi ~ 0 and drops out of the panel ranking;
    a genuinely specific pair scores high. Confidence carries the normalised
    variant (npmi, clamped to [0,1]). Read-time / on-demand — never a cron job.
    """
    total = conn.execute("SELECT COUNT(DISTINCT doc_id) FROM graph_mentions").fetchone()[0]
    if not total or total < 2:
        return 0
    docs_per = {r[0]: r[1] for r in conn.execute(
        "SELECT entity_id, COUNT(DISTINCT doc_id) FROM graph_mentions GROUP BY entity_id")}
    pairs = conn.execute(
        "SELECT src_id, dst_id, COUNT(DISTINCT doc_id) FROM graph_edges"
        " WHERE extractor='pmi' GROUP BY src_id, dst_id").fetchall()

    conn.execute("BEGIN IMMEDIATE")
    try:
        updated = 0
        for src, dst, n_ab in pairs:
            n_a, n_b = docs_per.get(src, 0), docs_per.get(dst, 0)
            if not n_a or not n_b or not n_ab:
                continue
            p_ab = n_ab / total
            pmi = math.log2((n_ab * total) / (n_a * n_b))
            npmi = 0.0 if p_ab >= 1 else pmi / -math.log2(p_ab)
            conn.execute(
                "UPDATE graph_edges SET weight=?, confidence=?"
                " WHERE src_id=? AND dst_id=? AND extractor='pmi'",
                (round(max(pmi, 0.0), 4), round(max(min(npmi, 1.0), 0.0), 4), src, dst))
            updated += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return updated


# ── co-retrieval (demoted to one signal among several) ───────────────────────

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
    """Write CO_ACTIVITY edges between the entities of the co-retrieved docs.

    Co-retrieval is now ONE weak signal (extractor ``co_activity``), not the
    whole relationship model. Provenance is the run itself — ``doc_id`` is
    ``run:<run_id>``, which joins straight back to the ``sessions`` row, so the
    edge stays citable. Bounded to ``max_entities`` so a wide result set cannot
    explode into O(n^2) edges. No model call.
    """
    doc_ids: list[str] = []
    for hit in hits:
        payload = hit.get("payload", hit)
        did = payload.get("doc_id") or payload.get("source_path") or payload.get("source_file")
        if did and did not in doc_ids:
            doc_ids.append(did)
    if not doc_ids:
        return 0

    marks = ",".join("?" * len(doc_ids))
    graph_ids = [r[0] for r in conn.execute(
        f"SELECT m.entity_id, MAX(e.confidence) AS conf FROM graph_mentions m"
        f" JOIN graph_entities e ON e.id = m.entity_id"
        f" WHERE m.doc_id IN ({marks}) GROUP BY m.entity_id"
        f" ORDER BY conf DESC, m.entity_id ASC LIMIT ?", (*doc_ids, max_entities)).fetchall()]

    provenance = f"run:{run_id}"
    graph_pairs = 0
    for i, src in enumerate(graph_ids):
        for dst in graph_ids[i + 1:]:
            a, b = (src, dst) if src < dst else (dst, src)
            add_edge(conn, a, b, "CO_ACTIVITY", confidence=0.4, weight=1.0,
                     doc_id=provenance, extractor="co_activity")
            graph_pairs += 1

    # v1 mirror (Sentinel stall canary + S4c hygiene check still read it)
    legacy_ids = _entity_ids_for_docs(conn, doc_ids, max_entities)
    legacy_pairs = [(a, b) for i, a in enumerate(legacy_ids) for b in legacy_ids[i + 1:]]
    if legacy_pairs:
        now = _now()
        conn.executemany(
            "INSERT INTO activity_edges(entity_a, entity_b, run_id, created_at)"
            " VALUES (?,?,?,?)", [(a, b, run_id, now) for a, b in legacy_pairs])
    conn.commit()
    return graph_pairs or len(legacy_pairs)


# ── health / render ──────────────────────────────────────────────────────────

def health(conn: sqlite3.Connection) -> dict:
    """Graph vitals for the Sentinel + Dev Mode line: counts, last-grown, 24h stall.

    Counts come from ``graph_*``; the v1 tables are the fallback so a workspace
    that has not been backfilled yet still reports honestly instead of "0".
    """
    ents = conn.execute("SELECT COUNT(*) FROM graph_entities").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    by_extractor = {r[0]: r[1] for r in conn.execute(
        "SELECT extractor, COUNT(*) FROM graph_edges GROUP BY extractor")}
    if not ents:
        ents = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    legacy_edges = conn.execute("SELECT COUNT(*) FROM activity_edges").fetchone()[0]
    if not edges:
        edges = legacy_edges

    stamps = [conn.execute("SELECT MAX(asserted_at) FROM graph_edges").fetchone()[0],
              conn.execute("SELECT MAX(created_at) FROM activity_edges").fetchone()[0]]
    last = max([s for s in stamps if s], default=None)

    stalled = True
    if last:
        try:
            then = datetime.strptime(last[:19], _ISO).replace(tzinfo=timezone.utc)
            stalled = (datetime.now(timezone.utc) - then) > timedelta(hours=24)
        except ValueError:
            stalled = False
    return {"entities": ents, "edges": edges, "by_extractor": by_extractor,
            "last_grown": last,
            "stalled_24h": bool(stalled and edges > 0) or (edges == 0)}


def top_entities(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Top-N entities for the read-only D3 panel, ranked by PMI-weighted degree.

    Junk is gone by construction (the extraction gate), and hub bias is gone by
    ranking: after :func:`mine_relationships` a token that co-occurs with
    everything carries ~0 weight and cannot hold a top slot.
    """
    # One pass over graph_edges (not a correlated subquery per entity) so the
    # panel stays inside the S4c 250ms budget as the graph grows.
    prior = " ".join(f"WHEN '{t}' THEN {w}" for t, w in PANEL_TYPE_PRIOR.items())
    rows = conn.execute(
        "WITH degree(id, score) AS ("
        "  SELECT id, SUM(w) FROM ("
        "    SELECT src_id AS id, weight AS w FROM graph_edges"
        "    UNION ALL SELECT dst_id, weight FROM graph_edges) GROUP BY id)"
        " SELECT e.id, e.label, e.type, e.confidence, e.mention_count,"
        f"        COALESCE(d.score, 0) * (CASE e.type {prior} ELSE 1.0 END) AS score"
        "   FROM graph_entities e LEFT JOIN degree d ON d.id = e.id"
        "  ORDER BY score DESC, e.mention_count DESC, e.confidence DESC, e.id ASC"
        "  LIMIT ?", (limit,)).fetchall()
    if rows:
        return [{"id": r[0], "label": r[1], "type": r[2], "score": round(float(r[5]), 3),
                 "confidence": r[3], "mentions": r[4]} for r in rows]

    # v1 fallback: a workspace whose graph has not been backfilled still renders
    # (a blank panel is a defect). These are v1 ids — entity_detail handles both.
    legacy = conn.execute(
        "SELECT id, label, type, confidence FROM entities ORDER BY confidence DESC LIMIT ?",
        (limit,)).fetchall()
    return [{"id": r[0], "label": r[1], "type": r[2], "score": 0, "confidence": r[3],
             "mentions": 0} for r in legacy]


def panel(conn: sqlite3.Connection, limit: int = 50) -> dict:
    """The read-only D3 panel payload: top-N nodes + the TYPED edges among them.

    The 50-node perf cap is enforced HERE, not in the HTTP layer — the S4c gate
    calls this function directly with limit=999 to prove no caller can widen it.
    """
    nodes = top_entities(conn, min(limit, 50))
    ids = [n["id"] for n in nodes]
    edges: list[dict] = []
    if ids:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT src_id, dst_id, predicate, SUM(weight) AS w, MAX(confidence),"
            f" MAX(doc_id), MAX(asserted_at), COUNT(*)"
            f" FROM graph_edges WHERE src_id IN ({marks}) AND dst_id IN ({marks})"
            f" GROUP BY src_id, dst_id, predicate"
            f" ORDER BY w DESC LIMIT 400", (*ids, *ids)).fetchall()
        edges = [{"source": r[0], "target": r[1], "predicate": r[2],
                  "weight": round(float(r[3] or 0.0), 3), "confidence": r[4],
                  "doc_id": r[5], "last_seen": r[6] or "", "sources": r[7]}
                 for r in rows]
    return {"nodes": nodes, "edges": edges, "health": health(conn)}


# ── drill-down ───────────────────────────────────────────────────────────────

def _receipts_for(conn: sqlite3.Connection, doc_id: str | None, limit: int = 5) -> list[dict]:
    if not doc_id:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT run_id, claim_text, chunk_id, retrieval_score, confidence_band, created_at"
        " FROM receipts WHERE doc_id=? ORDER BY id DESC LIMIT ?", (doc_id, limit)).fetchall()]


def _sessions_for(conn: sqlite3.Connection, run_ids: list[str]) -> list[dict]:
    if not run_ids:
        return []
    marks = ",".join("?" * len(run_ids))
    return [dict(r) for r in conn.execute(
        f"SELECT id, intent, started_at, duration, cost FROM sessions"
        f" WHERE id IN ({marks}) ORDER BY started_at DESC", run_ids).fetchall()]


def _legacy_detail(conn: sqlite3.Connection, entity_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, label, type, source_doc_id, confidence FROM entities WHERE id=?",
        (entity_id,)).fetchone()
    if row is None:
        return None
    run_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT run_id FROM activity_edges WHERE entity_a=? OR entity_b=?"
        " ORDER BY created_at DESC LIMIT 10", (entity_id, entity_id)).fetchall()]
    return {"id": row[0], "label": row[1], "type": row[2], "source_doc_id": row[3],
            "confidence": row[4], "runs": run_ids,
            "sessions": _sessions_for(conn, run_ids),
            "receipts": _receipts_for(conn, row[3]), "aliases": [], "mentions": 0,
            "related": []}


def entity_detail(conn: sqlite3.Connection, entity_id: int) -> dict | None:
    """Node click -> the entity + its receipts + the runs that touched it.

    Accepts either id space: ``graph_entities`` first, then the v1 mirror (whose
    ids live at/above :data:`LEGACY_ID_BASE`, so the two can never be confused).
    """
    row = conn.execute(
        "SELECT id, canonical_key, label, type, confidence, mention_count, first_seen,"
        " last_seen FROM graph_entities WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        return _legacy_detail(conn, entity_id)

    mentions = [dict(r) for r in conn.execute(
        "SELECT doc_id, chunk_id, confidence, created_at FROM graph_mentions"
        " WHERE entity_id=? ORDER BY created_at ASC LIMIT 25", (entity_id,)).fetchall()]
    source_doc_id = mentions[0]["doc_id"] if mentions else None
    run_ids = [r[0][4:] for r in conn.execute(
        "SELECT DISTINCT doc_id FROM graph_edges"
        " WHERE (src_id=? OR dst_id=?) AND extractor='co_activity' AND doc_id LIKE 'run:%'"
        " ORDER BY asserted_at DESC LIMIT 10", (entity_id, entity_id)).fetchall()]
    aliases = [r[0] for r in conn.execute(
        "SELECT alias FROM graph_aliases WHERE entity_id=? ORDER BY created_at", (entity_id,))]
    return {"id": row[0], "canonical_key": row[1], "label": row[2], "type": row[3],
            "confidence": row[4], "mentions": row[5], "first_seen": row[6],
            "last_seen": row[7], "source_doc_id": source_doc_id, "aliases": aliases,
            "mention_docs": mentions, "runs": run_ids,
            "sessions": _sessions_for(conn, run_ids),
            "receipts": _receipts_for(conn, source_doc_id),
            "related": _related(conn, entity_id)}


def _related(conn: sqlite3.Connection, entity_id: int, limit: int = 25) -> list[dict]:
    rows = conn.execute(
        "SELECT CASE WHEN g.src_id=? THEN g.dst_id ELSE g.src_id END AS other,"
        " CASE WHEN g.src_id=? THEN 'out' ELSE 'in' END AS direction,"
        " g.predicate, g.confidence, g.weight, g.doc_id, g.chunk_id, g.extractor,"
        " e.label, e.type"
        " FROM graph_edges g JOIN graph_entities e"
        "   ON e.id = CASE WHEN g.src_id=? THEN g.dst_id ELSE g.src_id END"
        " WHERE g.src_id=? OR g.dst_id=?"
        " ORDER BY g.weight DESC, g.confidence DESC LIMIT ?",
        (entity_id, entity_id, entity_id, entity_id, entity_id, limit)).fetchall()
    return [{"entity": {"id": r[0], "label": r[8], "type": r[9]}, "direction": r[1],
             "predicate": r[2], "confidence": r[3], "weight": r[4], "doc_id": r[5],
             "chunk_id": r[6], "extractor": r[7]} for r in rows]


def entity_profile(conn: sqlite3.Connection, key_or_id, workspace_id: str = "") -> dict | None:
    """Full profile for the entity API: identity, every alias, every mention doc,
    typed relations (each with its provenance) and the receipts of its home doc."""
    row = None
    if isinstance(key_or_id, int) or (isinstance(key_or_id, str) and key_or_id.isdigit()):
        row = conn.execute(
            "SELECT id, canonical_key, label, type, workspace_id, confidence, mention_count,"
            " first_seen, last_seen FROM graph_entities WHERE id=?", (int(key_or_id),)).fetchone()
    if row is None:
        key = canonical_key(str(key_or_id))
        params: list = [key, graph_resolve.alias_key(str(key_or_id))]
        clause = ""
        if workspace_id:
            clause = " AND workspace_id = ?"
            params.append(workspace_id)
        row = conn.execute(
            "SELECT id, canonical_key, label, type, workspace_id, confidence, mention_count,"
            " first_seen, last_seen FROM graph_entities"
            " WHERE (canonical_key = ? OR id IN (SELECT entity_id FROM graph_aliases"
            "        WHERE alias_key = ?))" + clause +
            " ORDER BY mention_count DESC LIMIT 1", params).fetchone()
    if row is None:
        return None
    if workspace_id and row[4] and row[4] != workspace_id:
        return None

    entity_id = row[0]
    aliases = [dict(r) for r in conn.execute(
        "SELECT alias, alias_key, source_doc_id, created_at FROM graph_aliases"
        " WHERE entity_id=? ORDER BY created_at", (entity_id,)).fetchall()]
    mention_docs = [dict(r) for r in conn.execute(
        "SELECT doc_id, chunk_id, confidence, created_at FROM graph_mentions"
        " WHERE entity_id=? ORDER BY created_at", (entity_id,)).fetchall()]
    home = mention_docs[0]["doc_id"] if mention_docs else None
    return {
        "entity": {"id": entity_id, "canonical_key": row[1], "label": row[2],
                   "type": row[3], "workspace_id": row[4], "confidence": row[5],
                   "mention_count": row[6], "first_seen": row[7], "last_seen": row[8]},
        "aliases": aliases,
        "mention_docs": mention_docs,
        "related": _related(conn, entity_id),
        "receipts": _receipts_for(conn, home, limit=10),
    }


def neighbors(conn: sqlite3.Connection, entity_id: int, hops: int = 2,
              limit: int = 50) -> list[dict]:
    """N-hop traversal via a RECURSIVE CTE (no graph library — lock L6).

    Every returned hop carries its predicate and its ``doc_id``, so a rendered
    path is auditable end to end.
    """
    hops = max(1, min(int(hops), 3))
    rows = conn.execute(
        "WITH RECURSIVE walk(id, hop, predicate, doc_id, chunk_id, extractor, via) AS ("
        "   SELECT ?, 0, NULL, NULL, NULL, NULL, NULL"
        "   UNION"
        "   SELECT CASE WHEN e.src_id = w.id THEN e.dst_id ELSE e.src_id END,"
        "          w.hop + 1, e.predicate, e.doc_id, e.chunk_id, e.extractor, w.id"
        "     FROM walk w JOIN graph_edges e ON e.src_id = w.id OR e.dst_id = w.id"
        "    WHERE w.hop < ?"
        ")"
        " SELECT w.id, e.label, e.type, w.hop, w.predicate, w.doc_id, w.chunk_id,"
        "        w.extractor, w.via"
        "   FROM walk w JOIN graph_entities e ON e.id = w.id"
        "  WHERE w.hop > 0 AND w.id <> ?"
        "  ORDER BY w.hop ASC, e.mention_count DESC LIMIT ?",
        (entity_id, hops, entity_id, limit)).fetchall()
    return [{"id": r[0], "label": r[1], "type": r[2], "hop": r[3], "predicate": r[4],
             "doc_id": r[5], "chunk_id": r[6], "extractor": r[7], "via": r[8]}
            for r in rows]
