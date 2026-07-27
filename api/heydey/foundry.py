"""Foundry — the Architect onboarding: 5 Qs -> 3-6 validated Layer-2 agents (S6c).

The load-bearing S6c invariant (L6/L13, non-negotiable): a tailored agent is a
**validated config row** in ``agent_specs``, hydrated into the EXISTING
:class:`pipeline.AgentSpec` and run through ``run_pipeline``. No generated code,
no freeform loop, no free-form prompt. The Foundry **selects and parametrizes**
from a pre-authored shelf.

Zero LLM here — every check is mechanical, every value comes from either the
:data:`INTERVIEW` enum or the :data:`_SHELF` template tables. The only LLM
calls in the product remain inside ``run_pipeline`` (Contract A already
governs them). Every prompt-injection surface is closed at *config write time*:

- ``company_name`` matches a strict regex (no braces/quotes/newlines) and only
  ever appears in display strings (``name``/``role``) — never in a prompt.
- ``role`` is display-only ("NEVER placed in any LLM prompt" — Contract §B).
- ``focus`` is lowercase alphanumeric + space + hyphen (regex-guarded); it is
  appended to the RETRIEVAL query only. Injection-inert by construction.

All-or-nothing writes (§A4): every spec must pass :func:`validate_spec` before
any row lands in ``agent_specs``; ANY failure aborts with ``FoundryError`` +
``onboard_failed`` event, ZERO rows in the table. Re-onboard is idempotent
(deterministic ids per playbook -> ``ON CONFLICT DO UPDATE ... version+=1``).

Naming discipline: ``agent_specs.validator_pass`` here records **spec
validation** (this file). It is DISTINCT from Contract-A **answer validation**
in ``run_pipeline`` — every run of these agents still gets that. Do not conflate.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
import time
from datetime import datetime, timezone

from . import connector_sync, models_config, models_state, pipeline

# ── The 5-question interview (single source of truth — the webapp reads it) ──

INTERVIEW: list[dict] = [
    {"key": "business_type", "label": "What kind of business is this?",
     "type": "choice", "options": ["d2c", "agency", "product"]},
    {"key": "company_name", "label": "What is the business called?",
     "type": "text", "pattern": r"^[A-Za-z0-9][A-Za-z0-9 .&'-]{1,39}$"},
    {"key": "primary_goal", "label": "What should the fleet do first?",
     "type": "choice", "options_by_playbook": {
        "d2c-ops":      ["daily_brief", "cited_answers", "cost_watch"],
        "agency-brief": ["client_briefs", "cited_answers"],
        "pm-product":   ["daily_brief", "cited_answers"]}},
    {"key": "sources", "label": "Which connected sources should it watch?",
     "type": "multi", "options_by_playbook": {
        "d2c-ops":      ["demo-shopify", "demo-sheets"],
        "agency-brief": ["demo-agency"],
        # pm sources are CORPUS THEMES (doc folders), not connectors — recorded
        # in foundry_events; the fixed trio watches all three themes in v1.
        "pm-product":   ["prds", "interviews", "competitor-notes"]}},
    {"key": "answer_style", "label": "How should answers read?",
     "type": "choice", "options": ["verbatim", "synthesized"]},
]

# ── Playbook shelf (pre-authored; the ONLY source of specs) ───────────────────
# Template tuple layout:
#   (id, name_template, role_template, k_base, k_cited, synthesize_by_style, focus)
#   - k_cited: bumped k when primary_goal == "cited_answers" (analysts only; else None)
#   - synthesize_by_style: True -> respect ⑤ answer_style; False -> always extractive

_PLAYBOOK_BY_BUSINESS_TYPE = {"d2c": "d2c-ops", "agency": "agency-brief",
                              "product": "pm-product"}

_SHELF: dict[str, dict] = {
    "d2c-ops": {
        "connectors": ["demo-shopify", "demo-sheets"],
        "core": [
            ("d2c-analyst", "{company} — Ops Analyst",
             "{company}: answers ops questions from synced order data — every sentence cited.",
             6, 8, True, "orders returns revenue sku"),
            ("d2c-librarian", "{company} — Source Librarian",
             "{company}: cites the exact source chunk for any ops question — verbatim slices only.",
             8, None, False, ""),
        ],
        "watch": {
            "demo-shopify": (
                "d2c-returns-watch", "{company} — Returns Watch",
                "{company}: flags return-rate spikes from synced order data — extractive, cited.",
                4, None, False, "rto returns rate sku orders"),
            "demo-sheets": (
                "d2c-spend-watch", "{company} — Spend Watch",
                "{company}: flags adspend and CAC drifts from synced sheets — extractive, cited.",
                4, None, False, "adspend spend cac channel"),
        },
        "goal": {
            "daily_brief": (
                "d2c-overnight", "{company} — Overnight Analyst",
                "{company}: summarises overnight ops changes from synced data — every sentence cited.",
                6, None, True, "yesterday overnight change orders spend"),
            "cost_watch": (
                "d2c-cost-watch", "{company} — Cost Watch",
                "{company}: watches spend and CAC against budget from synced sheets — extractive, cited.",
                4, None, False, "spend cac budget cost"),
        },
    },
    "pm-product": {
        # Doc-driven playbook (playbook_pm): the corpus IS the source — no
        # connectors, so no watch/goal minting; the spec'd trio ships as core
        # (fleet = exactly 3 by construction, the USE-CASES W1 shape).
        "connectors": [],
        "core": [
            ("pm-competitor-watch", "{company} — Competitor Watch",
             "{company}: flags competitor moves from ingested notes — extractive, cited.",
             6, None, False, "competitor pricing positioning launch"),
            ("pm-user-voice", "{company} — User Voice",
             "{company}: answers user-feedback questions from ingested interviews — every sentence cited.",
             6, 8, True, "interview user feedback friction onboarding"),
            ("pm-roadmap-risk", "{company} — Roadmap Risk",
             "{company}: flags roadmap risks and slips from ingested PRDs — extractive, cited.",
             6, None, False, "roadmap risk dependency deadline scope"),
        ],
        "watch": {},
        "goal": {},
    },
    "agency-brief": {
        "connectors": ["demo-agency"],
        "core": [
            ("agency-analyst", "{company} — Account Analyst",
             "{company}: answers account questions from synced intake data — every sentence cited.",
             6, 8, True, "client intake deliverable timeline budget"),
            ("agency-librarian", "{company} — Source Librarian",
             "{company}: cites the exact source chunk for any client question — verbatim slices only.",
             8, None, False, ""),
        ],
        "watch": {
            "demo-agency": (
                "agency-intake-watch", "{company} — Intake Watch",
                "{company}: flags new client intake requests from synced data — extractive, cited.",
                4, None, False, "intake new client brief request"),
        },
        "goal": {
            "client_briefs": (
                "agency-brief-drafter", "{company} — Brief Drafter",
                "{company}: answers creative-brief inputs from synced intake — every sentence cited.",
                8, None, True, "brand voice audience deliverable reference"),
        },
    },
}

_SHELF_IDS = frozenset(_SHELF.keys())

# ── Validation regexes / enums ────────────────────────────────────────────────

# id: lowercase alphanumeric + hyphen, 2-41 chars, no leading hyphen
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
# focus: lowercase alphanumeric + space + hyphen, 1-80 chars (or empty)
_FOCUS_RE = re.compile(r"^[a-z0-9][a-z0-9 -]{0,79}$")
# The S6c allowlist — widening it is a Fable amendment (see §A3 of the contract).
# Every profile routes "ask" today; an invented class silently falling through
# get_pair to default is exactly the drift this blocks.
_TASK_CLASS_ALLOWED = frozenset({"ask"})


class FoundryError(ValueError):
    """Fail-closed onboarding error: bad answers, unsynced source, or invalid spec."""


# ── helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_printable_line(s, min_len: int, max_len: int) -> bool:
    """Printable single-line string: no control chars, no newlines, length bounded.

    Rejects None / non-strings up-front so validate_spec can rely on the result."""
    if not isinstance(s, str):
        return False
    if not (min_len <= len(s) <= max_len):
        return False
    for c in s:
        if ord(c) < 32:  # covers \n, \r, \t, and all other C0 controls
            return False
    return True


def _log_event(conn: sqlite3.Connection, workspace_id: str, step: str,
               detail: dict) -> None:
    """Append one row to foundry_events. Audit-only — commits immediately so the
    row survives even if the onboarding raises next."""
    conn.execute(
        "INSERT INTO foundry_events(workspace_id, step, detail, created_at) "
        "VALUES (?, ?, ?, ?)",
        (workspace_id, step, json.dumps(detail, default=str), _now()),
    )
    conn.commit()


# ── validate_spec — deterministic per-spec config check ───────────────────────

def validate_spec(spec: dict) -> list[str]:
    """Deterministic per-spec check — no LLM. Empty list == valid; reasons otherwise.

    Batch uniqueness is caller-checked in :func:`instantiate` (one spec doesn't
    know the batch). Every rule here is single-spec (regex / enum / range) so
    a caller can validate one row at a time — the mechanical config gate that
    ``validator_pass`` on ``agent_specs`` records.
    """
    reasons: list[str] = []
    if not isinstance(spec, dict):
        return ["spec is not a dict"]

    # id — lowercase alphanumeric + hyphen, no leading hyphen, 2-41 chars
    sid = spec.get("id")
    if not isinstance(sid, str) or not _ID_RE.match(sid):
        reasons.append(f"id {sid!r} does not match ^[a-z0-9][a-z0-9-]{{1,40}}$")

    # name — printable single-line, 1-60 chars
    if not _is_printable_line(spec.get("name"), 1, 60):
        reasons.append("name must be a printable single-line string of 1-60 chars")

    # task_class — the S6c allowlist
    tc = spec.get("task_class")
    if tc not in _TASK_CLASS_ALLOWED:
        reasons.append(f"task_class {tc!r} not in {sorted(_TASK_CLASS_ALLOWED)}")

    # k — bounded int (bool is a subclass of int in Python; reject it)
    k = spec.get("k")
    if isinstance(k, bool) or not isinstance(k, int) or not (3 <= k <= 12):
        reasons.append(f"k {k!r} must be an int in [3, 12]")

    # synthesize — strict bool
    syn = spec.get("synthesize")
    if not isinstance(syn, bool):
        reasons.append(f"synthesize {syn!r} must be a bool")

    # focus — empty or ^[a-z0-9][a-z0-9 -]{0,79}$
    focus = spec.get("focus")
    if not isinstance(focus, str):
        reasons.append(f"focus must be a string (got {type(focus).__name__})")
    elif focus != "" and not _FOCUS_RE.match(focus):
        reasons.append(f"focus {focus!r} does not match ^[a-z0-9][a-z0-9 -]{{0,79}}$")

    # role — printable single-line, 1-140 chars
    if not _is_printable_line(spec.get("role"), 1, 140):
        reasons.append("role must be a printable single-line string of 1-140 chars")

    # playbook — must be one of the shelf ids
    pb = spec.get("playbook")
    if pb not in _SHELF_IDS:
        reasons.append(f"playbook {pb!r} not in {sorted(_SHELF_IDS)}")

    # Router resolves: the active profile can route this task_class + the family
    # rule holds. A broken profile fails at instantiate, not at first run.
    if tc in _TASK_CLASS_ALLOWED:
        try:
            pair = models_config.get_pair(models_state.active_profile(), tc)
            pair.check()
        except Exception as exc:
            reasons.append(f"router check failed: {exc}")

    return reasons


# ── corpus_scan — read-only workspace inventory ───────────────────────────────

def corpus_scan(conn: sqlite3.Connection, workspace_id: str) -> dict:
    """What a fresh Architect sees before committing to a fleet.

    Read-only, no LLM. Doc count comes from ``points`` (connector sync writes
    no ``docs`` rows), so counts reconcile with the retrievable corpus.
    ``clean_sources`` = registered connectors whose chunks actually landed —
    i.e. the sources the caller may legitimately pick as watch targets.
    """
    chunks = conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    docs = conn.execute(
        "SELECT COUNT(DISTINCT doc_id) FROM points "
        "WHERE doc_id IS NOT NULL AND doc_id != ''"
    ).fetchone()[0]
    entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    connector_rows = connector_sync.live_map(conn, workspace_id)
    clean_sources = [c["connector_id"] for c in connector_rows
                     if (c.get("chunks") or 0) > 0]
    # Doc-driven playbooks (pm-product) pick CORPUS THEMES, not connectors —
    # same rule, same phrase: a theme is clean only when its docs actually
    # landed in this workspace. "other" never qualifies as a pickable theme.
    from . import playbook_pm  # local import, mirrors morning_brief's pattern
    theme_rows = conn.execute(
        "SELECT DISTINCT COALESCE(source_file, doc_id) FROM points").fetchall()
    doc_themes = sorted({playbook_pm._theme_of(r[0]) for r in theme_rows if r[0]}
                        - {"other"})
    return {
        "chunks": chunks,
        "docs": docs,
        "entities": entities,
        "connectors": connector_rows,
        "clean_sources": clean_sources + doc_themes,
    }


# ── answer validation (strict, fail-closed) ───────────────────────────────────

def _validate_answers(answers: dict) -> str:
    """Strict validation against :data:`INTERVIEW`; returns the derived playbook.

    Fail-closed: unknown key, missing key, choice option not in the (possibly
    playbook-derived) enum, or ``company_name`` failing the pattern -> raise.
    The pattern bans braces / newlines / quotes — no prompt-injection surface.
    """
    if not isinstance(answers, dict):
        raise FoundryError("answers must be a dict")

    expected = {q["key"] for q in INTERVIEW}
    provided = set(answers.keys())
    unknown = provided - expected
    if unknown:
        raise FoundryError(f"unknown answer keys: {sorted(unknown)}")
    missing = expected - provided
    if missing:
        raise FoundryError(f"missing answer keys: {sorted(missing)}")

    # Derive playbook first — every context-dependent option maps off it.
    bt = answers["business_type"]
    bt_q = next(q for q in INTERVIEW if q["key"] == "business_type")
    if bt not in bt_q["options"]:
        # clinic / hospital render as "Phase-2 shelf" in the UI; the API refuses
        # them here (the interview enum is the closed set).
        raise FoundryError(
            f"business_type {bt!r} not supported yet — options: {bt_q['options']}"
        )
    playbook = _PLAYBOOK_BY_BUSINESS_TYPE[bt]

    reasons: list[str] = []
    for q in INTERVIEW:
        key = q["key"]
        if key == "business_type":
            continue  # already validated above
        val = answers[key]

        if q["type"] == "choice":
            options = q.get("options") or q["options_by_playbook"].get(playbook, [])
            if val not in options:
                reasons.append(f"{key} {val!r} not in {options}")

        elif q["type"] == "multi":
            options = q["options_by_playbook"].get(playbook, [])
            if not isinstance(val, list):
                reasons.append(f"{key} must be a list")
                continue
            if not val:
                # Empty sources would land a 2-agent fleet — violates N in [3, 6].
                reasons.append(f"{key} must be non-empty (pick at least one source)")
            for v in val:
                if v not in options:
                    reasons.append(f"{key} entry {v!r} not in {options}")

        elif q["type"] == "text":
            pat = q["pattern"]
            if not isinstance(val, str) or not re.match(pat, val):
                reasons.append(f"{key} {val!r} fails pattern {pat!r}")

    if reasons:
        raise FoundryError("; ".join(reasons))
    return playbook


# ── fleet construction (from the shelf, always) ───────────────────────────────

def _mint(playbook: str, template: tuple, company: str, cited_bump: bool,
          style_synth: bool) -> dict:
    tid, name_tmpl, role_tmpl, k_base, k_cited, syn_by_style, focus = template
    k = k_cited if (cited_bump and k_cited is not None) else k_base
    synthesize = style_synth if syn_by_style else False
    return {
        "id": tid,
        "name": name_tmpl.format(company=company),
        "role": role_tmpl.format(company=company),
        "task_class": "ask",
        "k": k,
        "synthesize": synthesize,
        "focus": focus,
        "playbook": playbook,
    }


def _build_fleet(playbook: str, answers: dict) -> list[dict]:
    """CORE (2-3) + one WATCH per chosen source (0-2) + GOAL specialist (0-1)
    -> N in [3, 6] by construction. Ids are fixed per playbook (idempotent
    re-onboard). ``{company}`` fills display strings only — never prompts."""
    shelf = _SHELF[playbook]
    company = answers["company_name"]
    goal = answers["primary_goal"]
    sources = answers["sources"]
    style_synth = answers["answer_style"] == "synthesized"
    cited_bump = goal == "cited_answers"

    specs: list[dict] = []
    for tmpl in shelf["core"]:
        specs.append(_mint(playbook, tmpl, company, cited_bump, style_synth))
    for src in sources:
        watch = shelf["watch"].get(src)
        if watch is not None:
            specs.append(_mint(playbook, watch, company, False, style_synth))
    goal_tmpl = shelf["goal"].get(goal)
    if goal_tmpl is not None:
        specs.append(_mint(playbook, goal_tmpl, company, False, style_synth))
    return specs


# ── all-or-nothing upsert (§A4 step 6) ────────────────────────────────────────

_SPEC_JSON_FIELDS = ("id", "name", "task_class", "k", "synthesize",
                     "role", "focus", "playbook")


def _row_to_out(row) -> dict:
    """Materialise a stored spec row into the return shape (row cols + hydrated
    spec fields). Callers get everything they need without re-parsing spec_json."""
    spec_json = json.loads(row["spec_json"])
    out = {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "spec_json": row["spec_json"],
        "validator_pass": row["validator_pass"],
        "created_at": row["created_at"],
    }
    for key in ("task_class", "k", "synthesize", "role", "focus", "playbook"):
        if key in spec_json:
            out[key] = spec_json[key]
    return out


def _upsert_specs(conn: sqlite3.Connection, specs: list[dict]) -> list[dict]:
    """One BEGIN IMMEDIATE around every spec. Rolls back on any error — all-or-nothing."""
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for spec in specs:
            spec_json = json.dumps({k: spec[k] for k in _SPEC_JSON_FIELDS})
            conn.execute(
                "INSERT INTO agent_specs(id, name, version, spec_json, "
                "                        validator_pass, created_at) "
                "VALUES (?, ?, 1, ?, 1, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  spec_json=excluded.spec_json, name=excluded.name, "
                "  validator_pass=1, version=agent_specs.version + 1",
                (spec["id"], spec["name"], spec_json, now),
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    ids = [s["id"] for s in specs]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, name, version, spec_json, validator_pass, created_at "
        f"FROM agent_specs WHERE id IN ({marks})", ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [_row_to_out(by_id[s["id"]]) for s in specs]  # preserve fleet order


# ── instantiate ───────────────────────────────────────────────────────────────

def instantiate(conn: sqlite3.Connection, workspace_id: str,
                answers: dict) -> list[dict]:
    """Fresh-workspace onboarding — deterministic questionnaire to a 3-6 agent fleet.

    Flow (§A4 in the contract):
      1. validate answers against INTERVIEW (strict, fail-closed)
      2. playbook from business_type; scan the workspace corpus
      3. every requested source must be in ``scan.clean_sources``
      4. build the fleet from the §A2 tables (N in [3, 6] by construction)
      5. validate_spec on EVERY spec (no writes yet) — batch uniqueness too
      6. one BEGIN IMMEDIATE for the upsert; any error rolls back all rows
      7. events: onboard_started -> corpus_scanned -> fleet_instantiated
                 (onboard_failed on any FoundryError)

    Idempotent: re-onboard with the same answers -> same ids, version += 1,
    ``agent_specs`` count stable.
    """
    t0 = time.perf_counter()
    _log_event(conn, workspace_id, "onboard_started", answers)
    try:
        playbook = _validate_answers(answers)

        scan = corpus_scan(conn, workspace_id)
        _log_event(conn, workspace_id, "corpus_scanned", scan)

        for src in answers["sources"]:
            if src not in scan["clean_sources"]:
                # UI's error-with-next-step: the caller must sync a source first.
                raise FoundryError(
                    f"sync a source first — connect step (no chunks from: {src};"
                    f" sync the connector or ingest that folder)"
                )

        specs = _build_fleet(playbook, answers)

        # Per-spec validate + batch uniqueness — no writes on any failure.
        all_reasons: list[str] = []
        ids_seen: set[str] = set()
        for spec in specs:
            reasons = validate_spec(spec)
            if spec["id"] in ids_seen:
                reasons.append(f"duplicate id in batch: {spec['id']}")
            ids_seen.add(spec["id"])
            if reasons:
                all_reasons.append(f"{spec.get('id')}: {'; '.join(reasons)}")
        if all_reasons:
            raise FoundryError("spec validation failed: " + " | ".join(all_reasons))

        rows = _upsert_specs(conn, specs)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        _log_event(conn, workspace_id, "fleet_instantiated",
                   {"n": len(rows), "ids": [s["id"] for s in specs],
                    "elapsed_ms": elapsed_ms})
        return rows
    except FoundryError as exc:
        _log_event(conn, workspace_id, "onboard_failed", {"error": str(exc)})
        raise


# ── hydrate / get_spec — turning stored rows back into AgentSpecs ─────────────

def hydrate(row_or_spec_json) -> pipeline.AgentSpec:
    """Turn a stored spec (agent_specs row or a parsed spec_json) into a
    :class:`pipeline.AgentSpec`.

    Forward-compat: unknown spec_json keys are IGNORED (dataclass-fields
    filtered). Back-compat: missing keys take dataclass defaults. A row-shaped
    input (has ``spec_json``) is decoded transparently.
    """
    if isinstance(row_or_spec_json, sqlite3.Row):
        data = dict(row_or_spec_json)
    elif isinstance(row_or_spec_json, dict):
        data = dict(row_or_spec_json)
    else:
        raise TypeError(
            f"hydrate expected Row or dict, got {type(row_or_spec_json).__name__}"
        )

    if "spec_json" in data:
        raw = data["spec_json"]
        spec_data = json.loads(raw) if isinstance(raw, str) else raw
    else:
        spec_data = data

    field_names = {f.name for f in dataclasses.fields(pipeline.AgentSpec)}
    filtered = {k: v for k, v in spec_data.items() if k in field_names}
    return pipeline.AgentSpec(**filtered)


def get_spec(conn: sqlite3.Connection, agent_id: str) -> pipeline.AgentSpec | None:
    """Return the hydrated spec, or None if missing / unvalidated (fail-closed).

    An ``agent_specs`` row with ``validator_pass != 1`` is NEVER hydrated — a
    hand-inserted or half-migrated row cannot leak into the runtime this way.
    """
    row = conn.execute(
        "SELECT id, name, version, spec_json, validator_pass, created_at "
        "FROM agent_specs WHERE id = ?", (agent_id,)
    ).fetchone()
    if row is None or row["validator_pass"] != 1:
        return None
    return hydrate(row)


# ── foundry_status — one-shot payload for the UI (single source of truth) ────

def foundry_status(conn: sqlite3.Connection, workspace_id: str) -> dict:
    """Full status: scan + stored specs + last-50 events + INTERVIEW.

    The webapp hardcodes no question text — it reads INTERVIEW from here. The
    phase is DERIVED from state (never persisted): specs > 0 -> ``fleet_live``;
    clean_sources > 0 -> ``sources_ready``; else ``empty``.
    """
    scan = corpus_scan(conn, workspace_id)
    spec_rows = conn.execute(
        "SELECT id, name, version, spec_json, validator_pass, created_at "
        "FROM agent_specs ORDER BY id"
    ).fetchall()
    specs = [_row_to_out(r) for r in spec_rows]
    events = [
        dict(e) for e in conn.execute(
            "SELECT id, step, detail, created_at FROM foundry_events "
            "WHERE workspace_id = ? ORDER BY id DESC LIMIT 50",
            (workspace_id,),
        ).fetchall()
    ]
    if specs:
        phase = "fleet_live"
    elif scan["clean_sources"]:
        phase = "sources_ready"
    else:
        phase = "empty"
    return {
        "workspace": workspace_id,
        "phase": phase,
        "scan": scan,
        "specs": specs,
        "events": events,
        "interview": INTERVIEW,
    }
