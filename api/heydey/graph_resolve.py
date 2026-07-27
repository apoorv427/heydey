"""Canonical entity identity + resolution (graph rebuild G1).

This module is the fix for the measured identity failure: v1 keyed entities on
``(label, type, source_doc_id)``, so one real-world thing became one node *per
document* — 26 nodes for a single product, 25 for another in the live store. Here the
key is ``(canonical_key, type)`` and every write goes through
:func:`resolve_entity` BEFORE the insert, so a node can only ever be created
once per real-world thing.

Four identity layers, cheapest first:

1. **canonical_key** — NFKC, possessive-stripped, punctuation-stripped,
   casefolded, whitespace-collapsed. "Acme's" and "  blueleaf " are one key.
2. **money normalisation** — value+unit arithmetic, not string matching:
   ``₹35.4L``, ``₹35,40,000`` and ``₹35.4 lakh`` all reduce to 3 540 000 INR and
   therefore to ONE node. A unit-less fragment below the money floor (a bare
   ``₹25``) is reported as a low-confidence candidate and never clears the node
   gate — those fragments were the top-ranked hubs in the old panel.
3. **alias lookup** — a surface form already recorded for an entity of the same
   type resolves to that entity.
4. **fuzzy merge** — stdlib :class:`difflib.SequenceMatcher` at ratio >= 0.92,
   blocked by first character + length bucket so the candidate set stays tiny.
   Never merges across types, never fires on short keys.

Pure stdlib + sqlite3 (lock L6). No third-party dependency, no model call.

Type reconciliation (G1b, measured 2026-07-27)
---------------------------------------------
The key above is ``(canonical_key, type)``, so identity still split when two
passes typed the SAME string differently: the deterministic pass calls every
gated proper noun ``org`` and the local-model pass asserts ``product`` /
``person`` / ``technology``. Measured on the live workspace after only 206 of
1,271 documents of the semantic pass: **386 canonical keys split across types,
792 rows** — "claude code" as org AND product AND technology, "blueleaf hq" as
org, place and product. Same duplicate-identity class as the 26-nodes bug, one
level up.

:func:`resolve_entity` therefore looks the key up across **all** types before it
mints anything, and reconciles to ONE row. The surviving type is chosen by an
explicit precedence, highest first:

======  ==========================  =============================================
tier    types                        why
======  ==========================  =============================================
4       money/date/lock/slice/       SHAPE, PROVEN — a closed regex or the curated
        marker at confidence         gazetteer literally matched the characters.
        >= 0.9                       Nothing semantic outranks the shape.
3       person/product/technology/   NAMED kinds — a specific reading of a name.
        place
2       org                          THE UNTYPED BUCKET. ``graph._candidate_pass``
                                     hardcodes ``org`` for every gated capitalised
                                     run, so "org" here means "a proper noun", not
                                     "an organisation". Vaguest label we mint.
1       decision/commitment          PROPOSITIONAL — a statement about a thing,
                                     not the thing's name.
0       money/date/lock/slice/       SHAPE, UNPROVEN — asserted without the regex
        marker below 0.9             that defines the type. Measured: this is
                                     where "quercetin"-as-marker and
                                     "cattle smuggling"-as-commitment live.
======  ==========================  =============================================

Ties break on: deterministic lane first (``confidence >= DETERMINISTIC_CONF``),
then mention_count, then confidence, then :data:`NAMED_ORDER` (an arbitrary but
FIXED order, so the outcome is reproducible), then the earliest row id.

:data:`DETERMINISTIC_CONF` = 0.9 is exact, not a guess: the deterministic lane
mints gazetteer 0.95 / lock 0.95 / slice 0.95 / money 0.95 / date 0.9 / marker
0.9, its proper-noun bucket tops out at 0.85, and ``graph_llm`` tops out at 0.8
(``CONF_RECURRING``). So ``>= 0.9`` means "a closed regex or the curated
gazetteer matched" and nothing else.

The rejected type is never discarded: every type ever asserted for an entity is
written to ``graph_aliases`` under the reserved :data:`TYPE_EVIDENCE_PREFIX`
key-space, so the panel can show "also asserted as person" and an auditor can
see what the merge overruled.

Guard rail — when NOT to merge (and its honest failure mode)
------------------------------------------------------------
Two rows are refused a merge when ALL THREE hold:

1. they share no source document (the two passes never read the same text), AND
2. both are NAMED-kind types (tier 3 or the ``org`` bucket), AND
3. both carry >= :data:`CORROBORATION_MIN` mentions.

That is the "a person and an unrelated company that share a string" case: two
independently well-attested named things that never co-occur.

The guard is therefore a **repair-time** rule (:func:`merge_type_splits`). At
resolve time the incoming assertion has no independent body of evidence to
weigh — condition 3 cannot hold for a row that does not exist yet — so
reconciliation always wins and the split never forms. Blocking there instead
would resurrect the defect: on the live corpus 173 of 386 groups share no
document at all, because the model asserts an entity in a chunk the gated
deterministic pass rejected in that same document.

**Failure mode, stated honestly — it fails in both directions.**

- *False merge*: a genuine homonym discussed inside ONE document (Ford the
  person and Ford the company in the same memo) is still merged. No model-free
  resolver can separate those, and this one does not pretend to.
- *False split*: one real thing whose two readings are both well attested and
  never co-occur stays split. Measured on the live workspace: 3 of the 386
  groups (``cctns`` org·6/product·3, ``dpdp`` org·21/product·3, ``location``
  org·3/place·3). Those are reported by :func:`merge_type_splits` as ``blocked``
  rather than hidden — a residual the panel can show, not a silent zero.

Doc-overlap alone was measured and rejected as the guard: 173 of the 386 groups
share no document at all (the model asserts an entity in a chunk the gated
deterministic pass rejected in that same document), so requiring overlap would
have closed only 55% of the gap.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

# Fixed vocabulary (shared contract). Extraction may only mint these types.
ENTITY_TYPES = frozenset({
    "person", "org", "product", "technology", "decision", "commitment",
    "money", "date", "place", "marker", "lock", "slice",
})

FUZZY_RATIO = 0.92        # merge threshold inside one type
FUZZY_MIN_LEN = 6         # short keys (L23 / S3 / ORION) are never fuzzy-merged
FUZZY_LEN_WINDOW = 2      # blocking bucket: |len(a) - len(b)| <= 2

# ── type precedence (see the module docstring for the measured rationale) ────
# A type whose shape a closed regex/gazetteer proved. Below DETERMINISTIC_CONF
# the SAME type name is the weakest evidence in the system, because the string
# demonstrably did not match the regex that defines it.
SHAPE_TYPES = frozenset({"money", "date", "lock", "slice", "marker"})
NAMED_TYPES = frozenset({"person", "product", "technology", "place"})
BUCKET_TYPE = "org"       # graph._candidate_pass's untyped proper-noun bucket

DETERMINISTIC_CONF = 0.9  # >= this <=> a closed regex or the curated gazetteer

TIER_SHAPE_PROVEN = 4
TIER_NAMED = 3
TIER_BUCKET = 2
TIER_PROPOSITIONAL = 1
TIER_SHAPE_UNPROVEN = 0

# Last-resort tiebreak inside TIER_NAMED. Arbitrary but FIXED: two rows that tie
# on evidence must still resolve the same way on every machine and every rerun.
NAMED_ORDER = {"product": 4, "person": 3, "technology": 2, "place": 1}

# A row needs this many mentions to count as "independently attested" for the
# no-merge guard. Measured: 3 leaves 3 of 386 live groups unmerged; 2 wrongly
# blocked 13 (including "claude" and "kritagya jha", plainly one thing each).
CORROBORATION_MIN = 3

# Reserved alias key-space recording every type ever asserted for an entity.
# Double colon: no canonicalised surface form produces one, and these rows are
# filtered out of the alias lists the UI renders (graph.entity_profile).
TYPE_EVIDENCE_PREFIX = "type::"

# The money floor: an amount with no magnitude unit and below this value is a
# fragment ("₹25"), not a business quantity. Measured: unit-less fragments were
# the single largest junk class in the rendered panel.
MONEY_FLOOR = 1000.0

_POSSESSIVE = re.compile(r"['’]s\b|['’]$")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")

_MONEY_UNITS = {
    "k": 1e3, "thousand": 1e3,
    "l": 1e5, "lac": 1e5, "lacs": 1e5, "lakh": 1e5, "lakhs": 1e5,
    "m": 1e6, "mn": 1e6, "million": 1e6,
    "cr": 1e7, "crore": 1e7, "crores": 1e7,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
}

# Rupee-prefixed amounts only — a bare number is a number, not money.
MONEY_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(crores?|cr|lakhs?|lacs?|thousand|million|billion|mn|bn|[lkmb])?\b",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── identity ─────────────────────────────────────────────────────────────────

def canonical_key(label: str) -> str:
    """Identity key for a surface form: NFKC, no possessive, no punctuation,
    casefolded, whitespace-collapsed. ``"Acme's"`` -> ``"blueleaf"``."""
    text = unicodedata.normalize("NFKC", str(label))
    text = _POSSESSIVE.sub("", text)
    text = _NON_WORD.sub(" ", text.casefold()).replace("_", " ")
    return _WS.sub(" ", text).strip()


def alias_key(label: str) -> str:
    """Key for a *surface form* (aliases). Deliberately lighter than
    :func:`canonical_key`: it keeps the possessive so "Acme" and
    "Acme's" are two recorded aliases of the one entity."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", str(label)).casefold()).strip()


def display_label(label: str) -> str:
    """Canonical display form: possessive dropped, edge punctuation trimmed."""
    text = unicodedata.normalize("NFKC", str(label)).strip()
    text = _POSSESSIVE.sub("", text)
    return _WS.sub(" ", text).strip(" ,.;:!?'\"-—–")


# ── money ────────────────────────────────────────────────────────────────────

def _compact(number: float) -> str:
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def format_money(value: float) -> str:
    """One canonical spelling per amount, so every spelling collapses to it."""
    if value >= 1e7:
        return f"₹{_compact(value / 1e7)}Cr"
    if value >= 1e5:
        return f"₹{_compact(value / 1e5)}L"
    if value >= 1e3:
        return f"₹{_compact(value / 1e3)}k"
    return f"₹{_compact(value)}"


def parse_money(label: str) -> dict | None:
    """``"₹35,40,000"`` -> ``{value: 3540000.0, unit: 'INR', label: '₹35.4L',
    key: 'inr:3540000.00', qualifies: True}``. ``None`` if it is not money.

    ``qualifies`` is False for a unit-less fragment under :data:`MONEY_FLOOR` —
    the caller keeps it as a low-confidence candidate but must not mint a node.
    """
    match = MONEY_RE.search(str(label))
    if match is None:
        return None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (match.group(2) or "").lower()
    value = amount * _MONEY_UNITS.get(unit, 1.0)
    return {
        "value": value,
        "unit": "INR",
        "magnitude": unit or None,
        "label": format_money(value),
        "key": f"inr:{value:.2f}",
        "qualifies": bool(unit) or value >= MONEY_FLOOR,
    }


def identity(label: str, entity_type: str) -> tuple[str, str]:
    """(canonical_key, canonical display label) for one surface form."""
    if entity_type == "money":
        parsed = parse_money(label)
        if parsed is not None:
            return parsed["key"], parsed["label"]
    return canonical_key(label), display_label(label)


# ── resolution ───────────────────────────────────────────────────────────────

def _alias_lookup(conn: sqlite3.Connection, label: str, entity_type: str) -> int | None:
    row = conn.execute(
        "SELECT a.entity_id FROM graph_aliases a JOIN graph_entities e ON e.id = a.entity_id"
        " WHERE a.alias_key = ? AND e.type = ? LIMIT 1",
        (alias_key(label), entity_type),
    ).fetchone()
    return row[0] if row else None


def fuzzy_lookup(conn: sqlite3.Connection, key: str, entity_type: str) -> int | None:
    """Near-duplicate inside ONE type, blocked by first char + length bucket."""
    if len(key) < FUZZY_MIN_LEN:
        return None
    rows = conn.execute(
        "SELECT id, canonical_key FROM graph_entities"
        " WHERE type = ? AND substr(canonical_key, 1, 1) = ?"
        "   AND length(canonical_key) BETWEEN ? AND ?",
        (entity_type, key[:1], len(key) - FUZZY_LEN_WINDOW, len(key) + FUZZY_LEN_WINDOW),
    ).fetchall()
    best_id, best_ratio = None, FUZZY_RATIO
    for row_id, row_key in rows:
        ratio = SequenceMatcher(None, key, row_key).ratio()
        if ratio >= best_ratio:
            best_id, best_ratio = row_id, ratio
    return best_id


# ── type reconciliation ──────────────────────────────────────────────────────

class _Candidate:
    """One (canonical_key, type) row competing to be the surviving identity.

    ``entity_id`` is None for the row about to be written — the incoming
    assertion competes on the same terms as the rows already stored.
    """

    __slots__ = ("entity_id", "type", "confidence", "mentions")

    def __init__(self, entity_id: int | None, type: str, confidence: float | None,
                 mentions: int = 0):
        self.entity_id = entity_id
        self.type = type
        self.confidence = float(confidence or 0.0)
        self.mentions = int(mentions or 0)

    @property
    def proven(self) -> bool:
        """True when a closed regex or the curated gazetteer typed this row."""
        return self.confidence >= DETERMINISTIC_CONF

    def tier(self) -> int:
        if self.type in SHAPE_TYPES:
            return TIER_SHAPE_PROVEN if self.proven else TIER_SHAPE_UNPROVEN
        if self.type == BUCKET_TYPE:
            return TIER_BUCKET
        if self.type in NAMED_TYPES:
            return TIER_NAMED
        return TIER_PROPOSITIONAL

    def rank(self) -> tuple:
        """Sort key — MAX wins. Documented in the module docstring."""
        return (self.tier(), int(self.proven), self.mentions, self.confidence,
                NAMED_ORDER.get(self.type, 0),
                -(self.entity_id if self.entity_id is not None else 1 << 62))


def type_precedence(type: str, confidence: float | None = None) -> int:
    """Public view of the tier a (type, confidence) pair earns. See the table
    in the module docstring."""
    return _Candidate(None, type, confidence).tier()


def _shares_document(conn: sqlite3.Connection, a_id: int, b_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM graph_mentions a JOIN graph_mentions b ON a.doc_id = b.doc_id"
        " WHERE a.entity_id = ? AND b.entity_id = ? LIMIT 1", (a_id, b_id)).fetchone() is not None


def _may_merge(conn: sqlite3.Connection, winner: _Candidate, loser: _Candidate) -> bool:
    """The guard rail. False only for two independently well-attested NAMED
    things that never share a document — see the module docstring's failure
    mode. A row that does not exist yet (entity_id None) is never blocked: it
    has no independent attestation to weigh."""
    named = {*NAMED_TYPES, BUCKET_TYPE}
    if winner.type not in named or loser.type not in named:
        return True
    if winner.mentions < CORROBORATION_MIN or loser.mentions < CORROBORATION_MIN:
        return True
    if winner.entity_id is None or loser.entity_id is None:
        return True
    return _shares_document(conn, winner.entity_id, loser.entity_id)


def record_type_evidence(conn: sqlite3.Connection, entity_id: int, type: str,
                         doc_id: str | None = None) -> None:
    """Keep a type assertion that lost (or won) as auditable evidence.

    Written into ``graph_aliases`` under :data:`TYPE_EVIDENCE_PREFIX` — no schema
    change, idempotent via UNIQUE(alias_key, entity_id), and filtered out of the
    alias lists the UI renders. Losing a type must never mean losing the fact
    that something asserted it."""
    conn.execute(
        "INSERT INTO graph_aliases(entity_id, alias, alias_key, source_doc_id, created_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(alias_key, entity_id) DO NOTHING",
        (entity_id, type, f"{TYPE_EVIDENCE_PREFIX}{type}", doc_id or "", _now()))


def fold_entity(conn: sqlite3.Connection, winner_id: int, loser_id: int) -> None:
    """Re-point everything the loser owns onto the winner, then delete it.

    Pure DML — no COMMIT — so it composes inside the caller's transaction.
    Collisions (the winner already has that mention / alias / edge) are dropped
    with ``UPDATE OR IGNORE`` + a sweep, never duplicated. PMI edge weights are
    re-derived by :func:`heydey.graph.mine_relationships`, so a dropped duplicate
    edge row costs a weight bump that the next mining pass recomputes anyway.

    One measured, bounded loss, stated rather than buried: ``graph_aliases`` is
    UNIQUE(alias_key, entity_id), so when both halves recorded the same
    alias_key under different CASING only one surface string can survive
    ("ACRYLAMIDE" and "Acrylamide" are one row). That is exactly what
    :func:`resolve_entity`'s ``ON CONFLICT DO NOTHING`` already does on a normal
    re-sighting, and no alias_key is ever lost — measured on the live merge:
    12,832 alias keys before, 12,832 after; 111 casing variants collapsed.
    """
    if winner_id == loser_id:
        return
    # A shared edge would become a self-loop after re-pointing. Kill it first —
    # a co-mention edge between two rows of the same thing is not a relationship.
    conn.execute("DELETE FROM graph_edges WHERE (src_id=? AND dst_id=?) OR (src_id=? AND dst_id=?)",
                 (winner_id, loser_id, loser_id, winner_id))
    conn.execute("UPDATE OR IGNORE graph_edges SET src_id=? WHERE src_id=?", (winner_id, loser_id))
    conn.execute("UPDATE OR IGNORE graph_edges SET dst_id=? WHERE dst_id=?", (winner_id, loser_id))
    conn.execute("DELETE FROM graph_edges WHERE src_id=? OR dst_id=?", (loser_id, loser_id))

    conn.execute("UPDATE OR IGNORE graph_mentions SET entity_id=? WHERE entity_id=?",
                 (winner_id, loser_id))
    conn.execute("DELETE FROM graph_mentions WHERE entity_id=?", (loser_id,))
    conn.execute("UPDATE OR IGNORE graph_aliases SET entity_id=? WHERE entity_id=?",
                 (winner_id, loser_id))
    conn.execute("DELETE FROM graph_aliases WHERE entity_id=?", (loser_id,))

    # earliest first_seen, latest last_seen, highest confidence — the loser's
    # history is absorbed, not overwritten.
    conn.execute(
        "UPDATE graph_entities SET"
        " confidence = MAX(COALESCE(confidence,0), COALESCE((SELECT confidence FROM"
        "   graph_entities WHERE id=?), 0)),"
        " first_seen = MIN(COALESCE(first_seen,''), COALESCE((SELECT first_seen FROM"
        "   graph_entities WHERE id=?), '')),"
        " last_seen = MAX(COALESCE(last_seen,''), COALESCE((SELECT last_seen FROM"
        "   graph_entities WHERE id=?), ''))"
        " WHERE id=?", (loser_id, loser_id, loser_id, winner_id))
    conn.execute("DELETE FROM graph_entities WHERE id=?", (loser_id,))
    conn.execute(
        "UPDATE graph_entities SET mention_count ="
        " (SELECT COUNT(*) FROM graph_mentions WHERE entity_id=?) WHERE id=?",
        (winner_id, winner_id))


def _load_siblings(conn: sqlite3.Connection, key: str) -> list[_Candidate]:
    return [_Candidate(r[0], r[1], r[2], r[3]) for r in conn.execute(
        "SELECT id, type, confidence, mention_count FROM graph_entities"
        " WHERE canonical_key = ?", (key,)).fetchall()]


def _reconcile_types(conn: sqlite3.Connection, key: str, type: str, same_id: int | None,
                     *, confidence: float, doc_id: str) -> int | None:
    """Fold every sibling of ``key`` into one row and return its id (None = mint).

    This is the fix for the 386 split keys: a second (canonical_key, type) row is
    never created for a thing that already has one under a different type.
    """
    siblings = [c for c in _load_siblings(conn, key)
                if c.type != type and c.entity_id != same_id]
    if not siblings:
        return same_id

    if same_id is None:
        incoming = _Candidate(None, type, confidence)      # the row about to be written
    else:
        # same_id may have been reached by alias/fuzzy lookup, so it is not
        # necessarily one of `key`'s rows — read it directly.
        row = conn.execute(
            "SELECT id, type, confidence, mention_count FROM graph_entities WHERE id=?",
            (same_id,)).fetchone()
        incoming = (_Candidate(row[0], row[1], max(float(row[2] or 0.0), confidence), row[3])
                    if row else _Candidate(same_id, type, confidence))
    candidates = [incoming, *siblings]
    winner = max(candidates, key=_Candidate.rank)

    if winner.entity_id is None:
        # The incoming type outranks everything stored: RETYPE the strongest
        # existing row instead of minting a sibling. UNIQUE(canonical_key, type)
        # is safe here — `same_id is None` means no row holds this type. The
        # guard cannot fire against a row that does not exist yet (see _may_merge).
        winner = max(siblings, key=_Candidate.rank)
        record_type_evidence(conn, winner.entity_id, winner.type, doc_id)
        conn.execute("UPDATE graph_entities SET type=? WHERE id=?", (type, winner.entity_id))
        winner = _Candidate(winner.entity_id, type, max(confidence, winner.confidence),
                            winner.mentions)

    for loser in candidates:
        if loser.entity_id is None or loser.entity_id == winner.entity_id:
            continue
        if not _may_merge(conn, winner, loser):
            continue                                      # documented residual split
        record_type_evidence(conn, winner.entity_id, loser.type, doc_id)
        fold_entity(conn, winner.entity_id, loser.entity_id)
    record_type_evidence(conn, winner.entity_id, type, doc_id)
    return winner.entity_id


def resolve_entity(conn: sqlite3.Connection, label: str, type: str, workspace_id: str,
                   *, confidence: float, doc_id: str, chunk_id: str | None = None) -> int:
    """Upsert one entity and record this sighting. Returns the entity id.

    ALWAYS called before an insert — that is the structural fix for the
    26-nodes-for-one-thing bug. Records the surface form in ``graph_aliases``,
    the sighting in ``graph_mentions``, refreshes ``mention_count``/``last_seen``
    and keeps the highest confidence seen.

    Since G1b it also reconciles ACROSS types: the key is looked up under every
    type before anything is minted, so two passes that type one string
    differently converge on one row instead of two (the 386-split bug). The
    surviving type follows the documented precedence; the rejected one is kept
    as evidence.
    """
    if type not in ENTITY_TYPES:
        raise ValueError(f"unknown entity type {type!r}; allowed: {sorted(ENTITY_TYPES)}")
    if not doc_id:
        raise ValueError("resolve_entity needs a doc_id (cite-or-silent applies to the graph)")
    if type == "money":
        # The money gate is structural, not a confidence threshold: a unit-less
        # fragment under the floor is refused here too, so a caller that skips
        # extract_entities (the llm pass) cannot mint a "₹25" node either.
        parsed = parse_money(label)
        if parsed is None or not parsed["qualifies"]:
            raise ValueError(f"money label {label!r} is a fragment, not an amount")
    key, display = identity(label, type)
    if not key:
        raise ValueError(f"label {label!r} has no canonical key")

    now = _now()
    row = conn.execute(
        "SELECT id FROM graph_entities WHERE canonical_key = ? AND type = ?", (key, type)
    ).fetchone()
    entity_id = row[0] if row else None
    if entity_id is None:
        entity_id = _alias_lookup(conn, label, type)
    if entity_id is None:
        entity_id = fuzzy_lookup(conn, key, type)
    # …and only then across types: reuse/retype an existing row for this key
    # rather than minting a same-key sibling.
    entity_id = _reconcile_types(conn, key, type, entity_id,
                                 confidence=confidence, doc_id=doc_id)

    if entity_id is None:
        cursor = conn.execute(
            "INSERT INTO graph_entities(canonical_key, label, type, workspace_id, confidence,"
            " mention_count, first_seen, last_seen) VALUES (?,?,?,?,?,0,?,?)",
            (key, display, type, workspace_id, confidence, now, now),
        )
        entity_id = int(cursor.lastrowid)
    else:
        conn.execute(
            "UPDATE graph_entities SET confidence = MAX(COALESCE(confidence, 0), ?),"
            " last_seen = ? WHERE id = ?",
            (confidence, now, entity_id),
        )

    conn.execute(
        "INSERT INTO graph_aliases(entity_id, alias, alias_key, source_doc_id, created_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(alias_key, entity_id) DO NOTHING",
        (entity_id, str(label).strip(), alias_key(label), doc_id, now),
    )
    # chunk_id defaults to '' (never NULL): SQLite treats NULLs as distinct in a
    # UNIQUE index, so a NULL chunk would defeat mention de-duplication.
    conn.execute(
        "INSERT INTO graph_mentions(entity_id, doc_id, chunk_id, confidence, created_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(entity_id, doc_id, chunk_id)"
        " DO UPDATE SET confidence = MAX(graph_mentions.confidence, excluded.confidence)",
        (entity_id, doc_id, chunk_id or "", confidence, now),
    )
    conn.execute(
        "UPDATE graph_entities SET mention_count ="
        " (SELECT COUNT(*) FROM graph_mentions WHERE entity_id = ?), last_seen = ?"
        " WHERE id = ?",
        (entity_id, now, entity_id),
    )
    return entity_id


def forget_document(conn: sqlite3.Connection, doc_id: str) -> None:
    """Drop everything one document asserted, then garbage-collect entities that
    no document mentions any more (with their edges + aliases). Makes re-ingest
    idempotent and keeps the panel free of orphan nodes."""
    touched = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM graph_mentions WHERE doc_id = ?", (doc_id,))]
    conn.execute("DELETE FROM graph_mentions WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM graph_edges WHERE doc_id = ?", (doc_id,))
    if not touched:
        return
    marks = ",".join("?" * len(touched))
    conn.execute(
        f"UPDATE graph_entities SET mention_count ="
        f" (SELECT COUNT(*) FROM graph_mentions m WHERE m.entity_id = graph_entities.id)"
        f" WHERE id IN ({marks})", touched)
    orphans = [r[0] for r in conn.execute(
        f"SELECT id FROM graph_entities WHERE id IN ({marks}) AND mention_count = 0", touched)]
    if orphans:
        omarks = ",".join("?" * len(orphans))
        conn.execute(f"DELETE FROM graph_edges WHERE src_id IN ({omarks})"
                     f" OR dst_id IN ({omarks})", (*orphans, *orphans))
        conn.execute(f"DELETE FROM graph_aliases WHERE entity_id IN ({omarks})", orphans)
        conn.execute(f"DELETE FROM graph_entities WHERE id IN ({omarks})", orphans)


# ── repair: fold the splits that already exist ───────────────────────────────

SPLIT_KEYS_SQL = ("SELECT canonical_key FROM graph_entities"
                  " GROUP BY canonical_key HAVING COUNT(DISTINCT type) > 1")


def split_type_keys(conn: sqlite3.Connection) -> list[str]:
    """Canonical keys that exist under more than one type — the defect count."""
    return [r[0] for r in conn.execute(SPLIT_KEYS_SQL + " ORDER BY canonical_key")]


def integrity_report(conn: sqlite3.Connection) -> dict:
    """Post-merge proof obligations. Every number here must be 0 except the
    counts — a non-zero ``dangling_*`` means the merge orphaned a reference."""
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "entities": one("SELECT COUNT(*) FROM graph_entities"),
        "edges": one("SELECT COUNT(*) FROM graph_edges"),
        "mentions": one("SELECT COUNT(*) FROM graph_mentions"),
        "split_type_keys": one(f"SELECT COUNT(*) FROM ({SPLIT_KEYS_SQL})"),
        "dangling_edge_src": one(
            "SELECT COUNT(*) FROM graph_edges g WHERE NOT EXISTS"
            " (SELECT 1 FROM graph_entities e WHERE e.id = g.src_id)"),
        "dangling_edge_dst": one(
            "SELECT COUNT(*) FROM graph_edges g WHERE NOT EXISTS"
            " (SELECT 1 FROM graph_entities e WHERE e.id = g.dst_id)"),
        "dangling_mentions": one(
            "SELECT COUNT(*) FROM graph_mentions m WHERE NOT EXISTS"
            " (SELECT 1 FROM graph_entities e WHERE e.id = m.entity_id)"),
        "dangling_aliases": one(
            "SELECT COUNT(*) FROM graph_aliases a WHERE NOT EXISTS"
            " (SELECT 1 FROM graph_entities e WHERE e.id = a.entity_id)"),
        "self_loop_edges": one("SELECT COUNT(*) FROM graph_edges WHERE src_id = dst_id"),
    }


def merge_type_splits(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Fold every existing (canonical_key, type) split into its precedence winner.

    ONE transaction for the whole run (``BEGIN IMMEDIATE``), so a reader never
    sees a half-merged graph and a crash leaves the store exactly as it was.
    Idempotent: a second run finds only the guarded groups and merges nothing.

    Returns ``{groups, merged, blocked, before, after}``. ``blocked`` lists the
    groups the guard rail refused — the honest residual, never hidden.
    """
    before = integrity_report(conn)
    keys = split_type_keys(conn)
    merged = 0
    blocked: list[dict] = []
    if not keys:
        return {"groups": 0, "merged": 0, "blocked": [], "dry_run": dry_run,
                "before": before, "after": before}

    conn.execute("BEGIN IMMEDIATE")
    try:
        for key in keys:
            rows = _load_siblings(conn, key)
            if len({c.type for c in rows}) < 2:
                continue
            winner = max(rows, key=_Candidate.rank)
            for loser in rows:
                if loser.entity_id == winner.entity_id:
                    continue
                if not _may_merge(conn, winner, loser):
                    blocked.append({"canonical_key": key, "kept": winner.type,
                                    "left_split": loser.type,
                                    "kept_mentions": winner.mentions,
                                    "left_mentions": loser.mentions})
                    continue
                record_type_evidence(conn, winner.entity_id, loser.type)
                if not dry_run:
                    fold_entity(conn, winner.entity_id, loser.entity_id)
                merged += 1
            record_type_evidence(conn, winner.entity_id, winner.type)
        # A 3-way fold can leave a winner->winner edge that fold_entity's
        # pairwise sweep did not see. Cite-or-silent: a self-loop is not a fact.
        conn.execute("DELETE FROM graph_edges WHERE src_id = dst_id")
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"groups": len(keys), "merged": merged, "blocked": blocked,
            "dry_run": dry_run, "before": before, "after": integrity_report(conn)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="python -m heydey.graph_resolve",
        description="Fold (canonical_key, type) identity splits into one entity each.")
    parser.add_argument("--merge-types", action="store_true", required=True,
                        help="run the type-split merge (the only mode today)")
    parser.add_argument("--workspace", default="blueleaf")
    parser.add_argument("--dry-run", action="store_true",
                        help="measure and roll back — changes nothing")
    args = parser.parse_args(argv)

    from . import workspaces

    conn = workspaces.connect(args.workspace)
    try:
        report = merge_type_splits(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    print(json.dumps(report, indent=2))
    after = report["after"]
    dangling = (after["dangling_edge_src"] + after["dangling_edge_dst"]
                + after["dangling_mentions"] + after["dangling_aliases"])
    return 1 if dangling else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    import sys

    sys.exit(main())
