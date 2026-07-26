"""Schema reservation for heydey.db — one file per workspace (S0 kickoff #5).

Every table a later slice fills is reserved here, empty, so a fresh workspace
db is born with the full shape:

- points / vec_points / fts_points (+ triggers): verbatim from the proven
  vector_store_v2.py (BLOS). S1 copies the 35,086 fastembed points in-place —
  the columns and the 384-dim cosine vec0 table must match exactly. Never re-embed.
- receipts: per-sentence proof ledger (v1.1 §14-A4).
- entities / relationships / activity_edges: graph tables, Executor Contract B verbatim.
- sessions: Session Browser (L25).
- connectors: MCP host registry (L31) — secrets stay in Keychain, only the ref here.
- docs / agent_specs / costs / approvals: ingest ledger, Foundry specs (L13),
  per-call cost telemetry, approvals lifecycle (L12/§14-A5).

Migration is versioned via schema_meta and idempotent (IF NOT EXISTS throughout).
"""

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 4  # v4 (S6c): + foundry_events — Architect onboarding stopwatch instrument

VECTOR_SIZE = 384  # BAAI/bge-small-en-v1.5 — same embedder as the migrating KB

# Tables that MUST exist in a fresh heydey.db (S0 done-gate item 4).
REQUIRED_TABLES = frozenset(
    {
        "schema_meta",
        # vector store (migrates in at S1)
        "points",
        "vec_points",
        "fts_points",
        # ingest + agents
        "docs",
        "agent_specs",
        # proof plane
        "receipts",
        "costs",
        "approvals",
        # graph (Executor Contract B)
        "entities",
        "relationships",
        "activity_edges",
        # session browser + connectors
        "sessions",
        "connectors",
        # MCP results (S1: ingest_guard extension; Contract C layer 1)
        "mcp_results",
        # Foundry onboarding audit (S6c) — the M:SS stopwatch instrument
        "foundry_events",
    }
)

_DDL = f"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---- vector store: verbatim from vector_store_v2.py (S1 copies points in-place) ----
CREATE TABLE IF NOT EXISTS points (
    rowid      INTEGER PRIMARY KEY,
    point_id   TEXT UNIQUE NOT NULL,
    doc_id     TEXT,
    source_file TEXT,
    created_at TEXT,
    text       TEXT,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_points_doc ON points(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS vec_points USING vec0(
    embedding float[{VECTOR_SIZE}] distance_metric=cosine
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_points USING fts5(
    text, content='points', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS points_fts_ai AFTER INSERT ON points BEGIN
    INSERT INTO fts_points(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS points_fts_ad AFTER DELETE ON points BEGIN
    INSERT INTO fts_points(fts_points, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS points_fts_au AFTER UPDATE OF text ON points BEGIN
    INSERT INTO fts_points(fts_points, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO fts_points(rowid, text) VALUES (new.rowid, new.text);
END;

-- ---- ingest ledger ----
CREATE TABLE IF NOT EXISTS docs (
    doc_id      TEXT PRIMARY KEY,
    source_path TEXT,
    title       TEXT,
    sha256      TEXT,
    bytes       INTEGER,
    status      TEXT,
    ingested_at TEXT
);

-- ---- Foundry: every Layer-2 agent is a validated config, never generated code ----
CREATE TABLE IF NOT EXISTS agent_specs (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    spec_json      TEXT NOT NULL,
    validator_pass INTEGER,
    created_at     TEXT
);

-- ---- proof plane ----
CREATE TABLE IF NOT EXISTS receipts (
    id              INTEGER PRIMARY KEY,
    run_id          TEXT,
    sentence_index  INTEGER,
    claim_text      TEXT,
    doc_id          TEXT,
    chunk_id        TEXT,
    retrieval_score REAL,
    confidence_band TEXT,
    validator_pass  INTEGER,
    model           TEXT,
    tokens          INTEGER,
    cost_usd        REAL,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_receipts_run ON receipts(run_id);

CREATE TABLE IF NOT EXISTS costs (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT,
    model       TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    cost_usd    REAL,
    latency_ms  REAL,
    tier_reason TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT,
    action_class TEXT,   -- outbound | spend | destructive
    payload      TEXT,
    status       TEXT,   -- pending | approved | denied
    requested_at TEXT,
    resolved_at  TEXT
);

-- ---- graph: Executor Contract B, verbatim ----
CREATE TABLE IF NOT EXISTS entities (
    id            INTEGER PRIMARY KEY,
    label         TEXT NOT NULL,
    type          TEXT,
    workspace_id  TEXT,
    source_doc_id TEXT,
    confidence    REAL,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS relationships (
    src_id    INTEGER,
    dst_id    INTEGER,
    rel_type  TEXT,
    weight    REAL,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS activity_edges (
    entity_a   INTEGER,
    entity_b   INTEGER,
    run_id     TEXT,
    created_at TEXT
);

-- ---- session browser ----
CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    intent            TEXT,
    started_at        TEXT,
    duration          REAL,
    cost              REAL,
    project           TEXT,
    workspace_id      TEXT,
    summary_embedding BLOB
);

-- ---- MCP results: raw connector content stored + tagged (Contract C layer 1) ----
-- Flagged rows are EXCLUDED from LLM context; the LLM sees sanitized_summary +
-- [connector_result_stored:<id>], never raw connector text.
CREATE TABLE IF NOT EXISTS mcp_results (
    id                 INTEGER PRIMARY KEY,
    connector_id       TEXT NOT NULL,
    tool               TEXT,
    received_at        TEXT,
    raw_text           TEXT,
    pii_redacted       INTEGER,
    injection_risk     INTEGER,
    injection_patterns TEXT,
    sanitized_summary  TEXT
);

-- ---- MCP connectors: keychain_ref only — never a credential value in this db ----
CREATE TABLE IF NOT EXISTS connectors (
    workspace_id    TEXT NOT NULL,
    connector_id    TEXT NOT NULL,
    keychain_ref    TEXT NOT NULL,
    scopes          TEXT,
    allowed_actions TEXT,
    PRIMARY KEY (workspace_id, connector_id)
);

-- ---- Morning Brief runs (S4b): items = JSON list, every item breadcrumbed ----
CREATE TABLE IF NOT EXISTS briefs (
    id           INTEGER PRIMARY KEY,
    workspace_id TEXT,
    kind         TEXT,     -- morning | manual
    items        TEXT,     -- JSON list of section/line/breadcrumb objects
    created_at   TEXT,
    notified     INTEGER DEFAULT 0
);

-- ---- Foundry onboarding audit (S6c): the M:SS stopwatch instrument ----
-- One row per step in an Architect run — `detail` carries the JSON payload
-- (answers, scan counts, fleet ids/elapsed_ms, or the error). Read by the
-- UI to compute "fleet live in M:SS" from real timestamps (D4 honesty rule).
--   step: onboard_started | corpus_scanned | fleet_instantiated
--       | onboard_failed  | first_answer
CREATE TABLE IF NOT EXISTS foundry_events (
    id           INTEGER PRIMARY KEY,
    workspace_id TEXT,
    step         TEXT,
    detail       TEXT,
    created_at   TEXT
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a workspace db to the current schema. Idempotent; safe on every open."""
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('created_at', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}
