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


def resolve_entity(conn: sqlite3.Connection, label: str, type: str, workspace_id: str,
                   *, confidence: float, doc_id: str, chunk_id: str | None = None) -> int:
    """Upsert one entity and record this sighting. Returns the entity id.

    ALWAYS called before an insert — that is the structural fix for the
    26-nodes-for-one-thing bug. Records the surface form in ``graph_aliases``,
    the sighting in ``graph_mentions``, refreshes ``mention_count``/``last_seen``
    and keeps the highest confidence seen.
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
