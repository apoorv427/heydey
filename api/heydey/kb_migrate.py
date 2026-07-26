"""One-time KB migration: copy an existing proven sqlite-vec store into a
workspace's heydey.db — IN PLACE, never re-embedded (lock L7).

Source is opened READ-ONLY (WAL shared read — safe while the live BLOS daemons
keep writing). Rows copy verbatim with their rowids preserved, so the
points <-> vec_points mapping holds and the FTS triggers rebuild fts_points
during the copy. Refuses a non-empty target: this is a migration, not a merge.

  .venv/bin/python -m heydey.kb_migrate --workspace blueleaf [--source PATH]
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

import sqlite_vec

from . import config, workspaces
from .schema import migrate

BATCH = 2000


def default_source() -> str | None:
    """The migration source is machine-local state (an existing store on the
    operator's disk), so it lives in corpus.json (`migration_source`;
    legacy key `blos_source` still honored), never in code."""
    cfg = config.load_corpus_config()
    return cfg.get("migration_source") or cfg.get("blos_source")


def open_source_readonly(path: str | Path) -> sqlite3.Connection:
    """Read-only connection to an external store, sqlite-vec loaded."""
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=30.0,
                           isolation_level=None)
    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.enable_load_extension(False)
    return conn


def _copy_batched(src: sqlite3.Connection, dst: sqlite3.Connection,
                  select_sql: str, insert_sql: str) -> int:
    copied, cursor = 0, -1
    while True:
        rows = src.execute(select_sql, (cursor, BATCH)).fetchall()
        if not rows:
            return copied
        dst.execute("BEGIN IMMEDIATE")
        try:
            dst.executemany(insert_sql, rows)
            dst.execute("COMMIT")
        except Exception:
            dst.execute("ROLLBACK")
            raise
        copied += len(rows)
        cursor = rows[-1][0]


def migrate_kb(source: str | Path, workspace_id: str) -> dict:
    src = open_source_readonly(source)
    dst = workspaces.connect(workspace_id)
    migrate(dst)  # ensure current schema (adds v2 tables to a v1 workspace)

    existing = dst.execute("SELECT count(*) FROM points").fetchone()[0]
    if existing:
        raise RuntimeError(
            f"target workspace {workspace_id!r} already holds {existing} points — "
            "migration copies into an EMPTY store only"
        )

    src_points = src.execute("SELECT count(*) FROM points").fetchone()[0]
    src_vectors = src.execute("SELECT count(*) FROM vec_points").fetchone()[0]

    copied_points = _copy_batched(
        src, dst,
        "SELECT rowid, point_id, doc_id, source_file, created_at, text, payload "
        "FROM points WHERE rowid > ? ORDER BY rowid LIMIT ?",
        "INSERT INTO points(rowid, point_id, doc_id, source_file, created_at, text, payload) "
        "VALUES (?,?,?,?,?,?,?)",
    )
    copied_vectors = _copy_batched(
        src, dst,
        "SELECT rowid, embedding FROM vec_points WHERE rowid > ? ORDER BY rowid LIMIT ?",
        "INSERT INTO vec_points(rowid, embedding) VALUES (?, ?)",
    )

    # ---- verify: counts, FTS rebuild, payload fidelity on a sample, integrity ----
    dst_points = dst.execute("SELECT count(*) FROM points").fetchone()[0]
    dst_vectors = dst.execute("SELECT count(*) FROM vec_points").fetchone()[0]
    dst_fts = dst.execute("SELECT count(*) FROM fts_points").fetchone()[0]

    sample_ok = True
    sample = src.execute(
        "SELECT point_id, payload FROM points ORDER BY rowid LIMIT 5 OFFSET ?",
        (max(0, src_points // 2),),
    ).fetchall()
    for point_id, payload in sample:
        row = dst.execute("SELECT payload FROM points WHERE point_id=?", (point_id,)).fetchone()
        if row is None or json.loads(row[0]) != json.loads(payload):
            sample_ok = False
    quick_check = dst.execute("PRAGMA quick_check").fetchone()[0]

    report = {
        "source": str(source),
        "workspace": workspace_id,
        "src_points": src_points,
        "src_vectors": src_vectors,
        "copied_points": copied_points,
        "copied_vectors": copied_vectors,
        "dst_points": dst_points,
        "dst_vectors": dst_vectors,
        "dst_fts_rows": dst_fts,
        "sample_payloads_identical": sample_ok,
        "quick_check": quick_check,
        "ok": (
            src_points == copied_points == dst_points == dst_fts
            and src_vectors == copied_vectors == dst_vectors
            and sample_ok
            and quick_check == "ok"
        ),
    }
    src.close()
    dst.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=default_source())
    parser.add_argument("--workspace", default="blueleaf")
    args = parser.parse_args()
    if not args.source:
        print("No migration source: pass --source PATH or set \"blos_source\" in "
              f"{config.corpus_config_path()} (see INSTALL.md).", file=sys.stderr)
        return 2
    report = migrate_kb(args.source, args.workspace)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
