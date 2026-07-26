"""Workspace registry + connection factory — STRUCTURAL isolation (S0 kickoff #2).

One SQLite file per workspace: ~/.heydey/workspaces/<id>/heydey.db. The safety
mechanism is the file boundary, never a workspace_id filter:

- connect(id) opens ONLY that workspace's file (URI mode=rw — never creates).
- ATTACH is denied at the connection level via the SQLite authorizer, so even
  raw SQL on a live connection cannot reach a second workspace file.
- Workspace ids are strictly validated (no traversal, no separators).
"""

import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

import sqlite_vec

from . import config
from .schema import SCHEMA_VERSION, migrate

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

DB_FILENAME = "heydey.db"


class WorkspaceError(ValueError):
    """Invalid workspace id or missing workspace."""


class WorkspaceExists(WorkspaceError):
    pass


class WorkspaceNotFound(WorkspaceError):
    pass


def validate_id(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or not _ID_RE.match(workspace_id):
        raise WorkspaceError(
            f"invalid workspace id {workspace_id!r} "
            "(lowercase letters, digits, '-', '_'; max 64 chars)"
        )
    return workspace_id


def db_path(workspace_id: str) -> Path:
    wid = validate_id(workspace_id)
    path = (config.workspaces_root() / wid / DB_FILENAME).resolve()
    root = config.workspaces_root().resolve()
    if root not in path.parents:  # belt over the regex's suspenders
        raise WorkspaceError(f"workspace path escapes root: {path}")
    return path


def list_workspaces() -> list[str]:
    root = config.workspaces_root()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if (d / DB_FILENAME).is_file())


def _authorizer(action, arg1, arg2, db_name, trigger):
    if action == sqlite3.SQLITE_ATTACH:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _open(path: Path, create: bool) -> sqlite3.Connection:
    uri = f"file:{quote(str(path))}?mode={'rwc' if create else 'rw'}"
    # isolation_level=None -> true autocommit; transactions are explicit
    # (BEGIN IMMEDIATE) where the write path needs them, matching the proven
    # vector-store concurrency pattern.
    conn = sqlite3.connect(uri, uri=True, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    conn.load_extension(sqlite_vec.loadable_path())
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.set_authorizer(_authorizer)
    # Version-check every open: a workspace created under an older schema gets the
    # new (idempotent, IF-NOT-EXISTS) DDL applied on first touch — found at S4b
    # when the live db, created at v2, had no `briefs` table.
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        current = int(row["value"]) if row else -1
    except sqlite3.OperationalError:  # no schema_meta at all (fresh or pre-schema file)
        current = -1
    if current != SCHEMA_VERSION:
        migrate(conn)
    return conn


def create_workspace(workspace_id: str) -> Path:
    """Create <root>/<id>/heydey.db with the full schema reserved. 0700 dirs."""
    path = db_path(workspace_id)
    if path.exists():
        raise WorkspaceExists(f"workspace {workspace_id!r} already exists")
    config.workspaces_root().mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.mkdir(mode=0o700)
    conn = _open(path, create=True)
    try:
        migrate(conn)
    finally:
        conn.close()
    return path


def connect(workspace_id: str) -> sqlite3.Connection:
    """Open ONLY the requested workspace's db. Refuses ids that don't exist."""
    path = db_path(workspace_id)
    if not path.is_file():
        raise WorkspaceNotFound(f"workspace {workspace_id!r} does not exist")
    return _open(path, create=False)
