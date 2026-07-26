"""``heydey init`` — from folders to a cited, gate-checked answer, timed (W1/B1).

Wires a new workspace end-to-end: writes/merges the machine-local
``~/.heydey/corpus.json`` (never clobbering other keys), creates the workspace,
ingests, runs the default S2 retrieval gate, and prints **TTFR** — the minutes
from this command to a correctly-cited answer. The activation metric, measured
on every install, not promised.

    heydey init --workspace myco --root ~/Documents/company-docs
    heydey init                    # interactive: prompts for folders

Non-interactive flags (CI/scripts): --root (repeatable) --glob --workspace --yes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config, ops_ingest, s2_gate, workspaces


def _merge_corpus_config(roots: list[tuple[str, str]]) -> Path:
    """Append (root, glob) pairs into corpus.json sources, preserving every
    other key and skipping exact duplicates."""
    path = config.corpus_config_path()
    cfg = config.load_corpus_config() if path.exists() else {}
    sources = cfg.get("sources", [])
    have = {(s.get("root"), s.get("glob")) for s in sources}
    for root, glob in roots:
        if (root, glob) not in have:
            sources.append({"root": root, "glob": glob})
    cfg["sources"] = sources
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="heydey init", description=__doc__)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--root", action="append", default=[],
                    help="folder to ingest (repeatable)")
    ap.add_argument("--glob", default="**/*.md")
    ap.add_argument("--yes", action="store_true", help="non-interactive")
    args = ap.parse_args(argv)
    t0 = time.time()

    roots = list(args.root)
    workspace = args.workspace
    if not roots and not args.yes:
        print("heydey init — point me at your documents (markdown/text folders).")
        while True:
            entry = input("folder path (empty line to finish): ").strip()
            if not entry:
                break
            roots.append(entry)
    if not workspace:
        workspace = (input("workspace name [myco]: ").strip() or "myco") \
            if not args.yes else "myco"
    if not roots:
        print("no folders given — nothing to do. See INSTALL.md.", file=sys.stderr)
        return 2

    expanded = []
    for r in roots:
        p = Path(r).expanduser()
        if not p.exists():
            print(f"skipping (not found): {p}", file=sys.stderr)
            continue
        expanded.append((str(p), args.glob))
    if not expanded:
        print("none of the given folders exist.", file=sys.stderr)
        return 2

    cfg_path = _merge_corpus_config(expanded)
    print(f"[1/4] corpus config: {cfg_path} (+{len(expanded)} source(s))")

    try:
        workspaces.connect(workspace).close()
        print(f"[2/4] workspace {workspace!r}: exists")
    except workspaces.WorkspaceNotFound:
        workspaces.create_workspace(workspace)
        print(f"[2/4] workspace {workspace!r}: created")

    report = ops_ingest.ingest_ops_corpus(workspace)
    print(f"[3/4] ingest: {report['files']} files · {report['chunks']} chunks · "
          f"{report['pii_redactions']} PII redactions · {len(report['errors'])} errors")

    print("[4/4] retrieval gate (default queries — write your own via corpus.json s2_failure_queries):")
    gate_green = s2_gate.run_gate(workspace)

    minutes = (time.time() - t0) / 60
    print(f"\nTTFR: {minutes:.1f} min from `heydey init` to a "
          f"{'cited, gate-checked answer — GREEN ✅' if gate_green else 'gate run (RED — tune queries/corpus) ❌'}")
    return 0 if (report["files"] and not report["errors"]) else 1


if __name__ == "__main__":
    sys.exit(main())
