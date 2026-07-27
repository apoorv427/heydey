"""Builder P — the F1 "provenance view" (build brief, Contract-adjacent).

Finder shows WHERE a file is. This module answers WHO made it, WHY, and from
WHAT SOURCES: the receipt object Heydey already writes on every approved
prepared action (see approvals.py), attached back onto the file on disk.

Two families of file, never conflated:

- Heydey-produced artifacts (``recent_artifacts``) — files under this
  workspace's ``artifacts/`` dir. Each is joined against ``approvals`` +
  ``receipts`` by exact path match (``receipts.doc_id`` is set to the
  artifact's path string at approval time — see ``approvals.decide``). A file
  with no matching row still lists; its provenance fields are ``None``, never
  guessed.
- OS files (``recent_os_files``) — a QUERY-TIME-ONLY ``mdfind`` shell-out
  (same shell-out pattern as ``find_cli.py``), scoped to a few named roots.
  We did NOT make these files, so ``source`` is stamped ``"os"`` and every
  provenance field is ``None``. No watcher, no daemon, no persisted index —
  one blocking subprocess call per configured root, per request.

Safety: metadata only, never file contents. Every OS-file candidate is
resolved and re-checked against its scan root (path-traversal / symlink
escape refused) and against ``ops_ingest._excluded`` (the Personal hard
wall) before it is allowed to appear.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config, ops_ingest, workspaces

# Default scan roots for recent_os_files; overridable via corpus.json's
# "artifact_roots" key (machine-local, never hardcoded operator paths).
_DEFAULT_ARTIFACT_ROOTS = ("~/Downloads", "~/Documents")

_KIND_BY_EXT = {"csv": "csv", "md": "md", "markdown": "md", "pdf": "pdf"}


def _kind(path: Path) -> str:
    return _KIND_BY_EXT.get(path.suffix.lower().lstrip("."), "other")


def _contained(path: Path, root: Path) -> bool:
    """True iff ``path`` resolves to something inside ``root``. Following
    symlinks to their real target is the point here: a symlink that LOOKS
    like it's under root but points outside must resolve to "outside" and
    be refused, exactly like a literal ``../`` escape."""
    try:
        resolved = path.resolve(strict=False)
        root_resolved = Path(root).expanduser().resolve(strict=False)
    except OSError:
        return False
    return resolved == root_resolved or root_resolved in resolved.parents


def _artifact_roots(roots: list | None) -> list[Path]:
    if roots is not None:
        raw = roots
    else:
        raw = config.load_corpus_config().get("artifact_roots") or list(_DEFAULT_ARTIFACT_ROOTS)
    return [Path(r).expanduser() for r in raw]


def _mdfind_lines(root: Path, since_days: int) -> list[str]:
    """One blocking mdfind call, scoped to ``root``. Any failure (mdfind
    missing, times out, non-zero exit) degrades to no results — never raises,
    never falls back to a background process or a persisted index."""
    try:
        proc = subprocess.run(
            ["mdfind", "-onlyin", str(root),
             f"kMDItemFSContentChangeDate >= $time.today(-{since_days})"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def recent_artifacts(conn: sqlite3.Connection, workspace_id: str, *, limit: int = 50) -> list[dict]:
    """Heydey-produced artifacts, newest first, each carrying whatever
    provenance we can actually source — never fabricated."""
    workspaces.validate_id(workspace_id)
    artifacts_dir = config.workspaces_root() / workspace_id / "artifacts"
    if not artifacts_dir.is_dir():
        return []

    entries: list[tuple[float, Path, int]] = []
    for path in artifacts_dir.iterdir():
        if not path.is_file() or not _contained(path, artifacts_dir):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, path, stat.st_size))
    entries.sort(key=lambda e: e[0], reverse=True)
    entries = entries[:limit]

    out = []
    for mtime, path, size in entries:
        # approvals.decide() sets receipts.doc_id to str(artifact_path) exactly
        # as constructed here, so an exact-string join is the honest link.
        row = conn.execute(
            "SELECT run_id, claim_text, created_at FROM receipts WHERE doc_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (str(path),),
        ).fetchone()

        run_id = approval_id = title = risk_tier = None
        receipt = None
        sources: list = []
        if row is not None:
            run_id = row["run_id"]
            receipt = {"claim_text": row["claim_text"], "created_at": row["created_at"]}
            if run_id and run_id.startswith("approval-") and run_id[len("approval-"):].isdigit():
                approval_id = int(run_id[len("approval-"):])
            if approval_id is not None:
                arow = conn.execute(
                    "SELECT payload, risk_tier FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                if arow is not None:
                    payload = json.loads(arow["payload"]) if arow["payload"] else {}
                    title = payload.get("title")
                    risk_tier = arow["risk_tier"]
                    sources = payload.get("sources") or []

        out.append({
            "path": str(path),
            "name": path.name,
            "kind": _kind(path),
            "bytes": size,
            "created_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            "source": "heydey",
            "run_id": run_id,
            "approval_id": approval_id,
            "title": title,
            "risk_tier": risk_tier,
            "receipt": receipt,
            "sources": sources,
        })
    return out


def recent_os_files(*, roots: list | None = None, limit: int = 30, since_days: int = 14) -> list[dict]:
    """Query-time-only mdfind scan of a few named roots. We never made these
    files: source is always "os", every provenance field is always None."""
    found: list[tuple[float, Path, int]] = []
    for root in _artifact_roots(roots):
        if not root.is_dir():
            continue
        for raw in _mdfind_lines(root, since_days):
            path = Path(raw)
            if not _contained(path, root):
                continue  # path traversal / symlink escape
            if ops_ingest._excluded(path):
                continue  # Personal hard wall
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            found.append((stat.st_mtime, path, stat.st_size))
    found.sort(key=lambda e: e[0], reverse=True)
    found = found[:limit]

    return [
        {
            "path": str(path),
            "name": path.name,
            "kind": _kind(path),
            "bytes": size,
            "created_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            "source": "os",
            "run_id": None,
            "approval_id": None,
            "title": None,
            "risk_tier": None,
            "receipt": None,
            "sources": [],
        }
        for mtime, path, size in found
    ]


def artifact_summary(conn: sqlite3.Connection, workspace_id: str) -> dict:
    """The 4-state UI signal: state is one of loaded | empty | error, each
    carrying a real next step (empty and error states must never hand the UI
    a blank panel)."""
    roots_configured = [str(r) for r in _artifact_roots(None)]
    try:
        heydey_items = recent_artifacts(conn, workspace_id, limit=10_000)
        os_items = recent_os_files(limit=10_000)
    except Exception as exc:  # noqa: BLE001 — surfaced as an honest error state, never a crash
        return {
            "state": "error",
            "heydey_count": 0,
            "os_count": 0,
            "roots_configured": roots_configured,
            "next_step": f"Could not read artifacts for workspace {workspace_id!r} ({exc}). "
                         "Check workspace access and retry.",
        }

    heydey_count = len(heydey_items)
    os_count = len(os_items)
    if heydey_count == 0 and os_count == 0:
        return {
            "state": "empty",
            "heydey_count": 0,
            "os_count": 0,
            "roots_configured": roots_configured,
            "next_step": "Approve a prepared action to create your first Heydey artifact, "
                         'or set corpus.json "artifact_roots" to surface recent OS files here.',
        }

    return {
        "state": "loaded",
        "heydey_count": heydey_count,
        "os_count": os_count,
        "roots_configured": roots_configured,
        "next_step": "",
    }
