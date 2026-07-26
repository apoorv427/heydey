"""Ops-corpus ingestion (S2) — the measured root cause of the baseline failures:
LOCKS ledgers, memory topic files, STATUS, and the founder doctrine were never
in the KB, so ops questions retrieved research PDFs instead of decisions.

Pipeline per file (per-doc error handling — one bad doc skips, never aborts):
    read -> slicer v2 -> ingest_guard (PII scrub + injection tag) -> store_chunks

HARD WALL (doctrine red line + the memory files' own instructions):
- anything under a Personal/ directory is never read
- personal/health memory files are excluded by name
- re-runs are idempotent: each doc is delete_document()-ed then re-stored.

  .venv/bin/python -m heydey.ops_ingest --workspace blueleaf
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, graph, workspaces
from .ingest_guard import guard_chunks
from .slicer import slice_document
from .vector_store import delete_document, store_chunks

# Which folders feed the ops corpus is MACHINE-LOCAL state, not source code:
# it lives in HEYDEY_HOME/corpus.json (see INSTALL.md), so the repo carries no
# operator's personal paths or filenames. When this module attribute is set
# (tests monkeypatch it; embedders may inject), it overrides the config file.
CORPUS_SOURCES: list[tuple[Path, str]] | None = None

# Structural Personal hard wall — POLICY, so it stays in code: any Personal/
# directory, any "personal" filename, and the agent-memory index (its one-line
# summaries reference personal entries). Installation-specific deny names
# (e.g. a founder's private topic files) belong in corpus.json `deny_names`.
DENY_NAMES = {"MEMORY.md"}
MAX_BYTES = 1_000_000


def _config_sources() -> list[tuple[Path, str]]:
    cfg = config.load_corpus_config()
    return [
        (Path(s["root"]).expanduser(), s.get("glob", "*.md"))
        for s in cfg.get("sources", [])
    ]


def _active_sources() -> list[tuple[Path, str]]:
    return CORPUS_SOURCES if CORPUS_SOURCES is not None else _config_sources()


def _deny_names() -> set[str]:
    return DENY_NAMES | set(config.load_corpus_config().get("deny_names", []))


def _excluded(path: Path, deny: set[str] | None = None) -> bool:
    if path.name in (deny if deny is not None else _deny_names()):
        return True
    if "personal" in path.name.lower():
        return True
    return any(part.lower() == "personal" for part in path.parts)


def iter_corpus_files() -> list[Path]:
    deny = _deny_names()
    seen: set[Path] = set()
    for root, pattern in _active_sources():
        if not root.exists():
            continue
        for path in sorted(root.glob(pattern)):
            if path.is_file() and path not in seen and not _excluded(path, deny):
                seen.add(path)
    return sorted(seen)


def ingest_ops_corpus(workspace_id: str) -> dict:
    conn = workspaces.connect(workspace_id)
    report = {"files": 0, "chunks": 0, "entities": 0, "pii_redactions": 0,
              "flagged_chunks": 0, "skipped": [], "errors": []}
    try:
        for path in iter_corpus_files():
            try:
                if path.stat().st_size > MAX_BYTES:
                    report["skipped"].append(f"{path} (> {MAX_BYTES} bytes)")
                    continue
                text = path.read_text(errors="replace")
                if not text.strip():
                    report["skipped"].append(f"{path} (empty)")
                    continue
                doc_id = str(path)
                created = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds")
                chunks = slice_document(
                    text, doc_id=doc_id, title=path.stem,
                    source_file=str(path), created_at=created,
                )
                chunks = guard_chunks(chunks)
                delete_document(conn, doc_id)  # idempotent re-ingest
                stored = store_chunks(conn, chunks, doc_id=doc_id)
                # entities @ ingest (Contract B): byproduct of the SAME loop, per-doc
                # isolated — index_document swallows its own errors so a bad parse
                # can never abort ingest or corrupt the graph.
                ents = graph.index_document(conn, doc_id, text, workspace_id)
                report["files"] += 1
                report["chunks"] += stored
                report["entities"] += ents
                report["pii_redactions"] += sum(c.get("pii_redacted", 0) for c in chunks)
                report["flagged_chunks"] += sum(1 for c in chunks if c.get("injection_risk"))
            except Exception as exc:  # per-doc: skip + log, keep going
                report["errors"].append(f"{path}: {exc}")
    finally:
        conn.close()
    return report


_EXAMPLE_CONFIG = """{
  "sources": [
    {"root": "~/Documents/company-docs", "glob": "**/*.md"},
    {"root": "~/Documents/decisions",    "glob": "*.md"}
  ],
  "deny_names": ["private-notes.md"]
}"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="blueleaf")
    args = parser.parse_args()
    if not _active_sources():
        print(
            f"No corpus sources configured.\n"
            f"Create {config.corpus_config_path()} (see INSTALL.md), e.g.:\n{_EXAMPLE_CONFIG}",
            file=sys.stderr,
        )
        return 2
    try:
        workspaces.connect(args.workspace).close()
    except workspaces.WorkspaceNotFound:
        workspaces.create_workspace(args.workspace)  # first ingest bootstraps the workspace
    report = ingest_ops_corpus(args.workspace)
    print(json.dumps(report, indent=2))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
