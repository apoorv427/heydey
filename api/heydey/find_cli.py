"""``heydey find`` — semantic file search from the terminal (build doc §4.1).

Searches MEANING over the workspace corpus (sqlite-vec + FTS5 hybrid), where
Spotlight matches filenames/raw tokens. Prints doc-level results with the exact
breadcrumb (file · chunk · date · score) so every hit is checkable.

Usage:
    python -m heydey.find_cli "pricing decision memo"      # search
    python -m heydey.find_cli "validator rule" --json      # machine-readable
    python -m heydey.find_cli "..." --workspace blueleaf --k 5
    python -m heydey.find_cli "..." --spotlight            # side-by-side vs mdfind
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from . import ask, workspaces


find = ask.find_docs  # one grouping implementation, shared with /find and the Library bar


def _spotlight(query: str, k: int) -> list[str]:
    """What Spotlight returns for the same query (content search, home-scoped)."""
    try:
        proc = subprocess.run(
            ["mdfind", "-onlyin", str(Path.home()), query],
            capture_output=True, text=True, timeout=15,
        )
        return [line for line in proc.stdout.splitlines() if line.strip()][:k]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"(mdfind failed: {exc})"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="heydey find", description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--workspace", default="blueleaf")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--spotlight", action="store_true",
                        help="also run the same query through mdfind, side by side")
    args = parser.parse_args(argv)

    conn = workspaces.connect(args.workspace)
    try:
        t0 = time.perf_counter()
        results = find(conn, args.query, k=args.k)
        elapsed_ms = (time.perf_counter() - t0) * 1000
    finally:
        conn.close()

    if args.as_json:
        print(json.dumps({"query": args.query, "latency_ms": round(elapsed_ms, 1),
                          "documents": results}, indent=2))
        return 0

    print(f"heydey find · {args.query!r} · {len(results)} docs · {elapsed_ms:.0f}ms")
    for i, doc in enumerate(results, 1):
        name = (doc["path"] or doc["source"]).rsplit("/", 1)[-1]
        score = f"{doc['best_score']:.3f}" if doc["best_score"] is not None else "—"
        print(f"\n{i}. {name}   [{doc['date']} · score {score}]")
        if doc["path"]:
            print(f"   {doc['path']}")
        for chunk in doc["chunks"]:
            snippet = " ".join(chunk["snippet"].split())[:120]
            print(f"   [chunk {chunk['chunk']}] {snippet}")

    if args.spotlight:
        print(f"\n--- Spotlight (mdfind, same query) ---")
        lines = _spotlight(args.query, args.k)
        if lines:
            for line in lines:
                print(f"   {line}")
        else:
            print("   (no results)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
