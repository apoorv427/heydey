"""S2 done-gate runner — THE gate (build doc §6/§7: non-negotiable).

The documented baseline-failure queries for THIS installation's corpus must
return the CORRECT chunk in the top-3 with a working breadcrumb (source path
resolves on disk). The real query set is machine-local state — corpus.json
`s2_failure_queries` (each: query, expect, markers[regex], require) — because
gate queries describe the operator's own corpus; the in-repo default is a
synthetic pair that works against a corpus containing Heydey's own docs.

Correctness is content-marker based (falsifiable), not filename-based, so a
correct chunk from any legitimately ingested source passes and a wrong chunk
cannot sneak through on a filename match.

  .venv/bin/python -m heydey.s2_gate --workspace blueleaf
"""

import argparse
import re
import sys
from pathlib import Path

from . import config, workspaces
from .ask import _citation, retrieve

DEFAULT_GATE_QUERIES = [
    {
        "query": "what does the wrapper ban forbid",
        "expect": "the banned-dependency list (hand-built core)",
        "markers": [r"langchain", r"banned|ban list|hand-built"],
        "require": 1,  # any one marker
    },
    {
        "query": "cross-model validator rule",
        "expect": "executor family != validator family, fail-closed",
        "markers": [r"famil", r"fail[\s-]?closed|validator"],
        "require": 2,  # both
    },
]


def _gate_queries() -> list[dict]:
    """Machine-local set from corpus.json, else the synthetic defaults."""
    raw = config.load_corpus_config().get("s2_failure_queries") or DEFAULT_GATE_QUERIES
    return [{**spec, "markers": [re.compile(m, re.I) for m in spec["markers"]]}
            for spec in raw]


def _chunk_matches(text: str, spec: dict) -> bool:
    hits = sum(1 for marker in spec["markers"] if marker.search(text))
    return hits >= spec["require"]


def run_gate(workspace_id: str, k: int = 3) -> bool:
    conn = workspaces.connect(workspace_id)
    all_pass = True
    for spec in _gate_queries():
        hits = retrieve(conn, spec["query"], k=k)
        matched_rank = None
        for rank, hit in enumerate(hits, 1):
            if _chunk_matches(hit["payload"].get("text", ""), spec):
                matched_rank = rank
                break
        breadcrumb_ok = False
        if matched_rank is not None:
            citation = _citation(hits[matched_rank - 1])
            breadcrumb_ok = (
                bool(citation["source"])
                and Path(citation["path"]).is_file()  # clickable = resolves on disk
                and citation["snippet"] != ""
            )
        status = "PASS" if (matched_rank and breadcrumb_ok) else "FAIL"
        all_pass &= status == "PASS"
        print(f"[{status}] {spec['query']!r} -> expect {spec['expect']}")
        if matched_rank:
            citation = _citation(hits[matched_rank - 1])
            print(f"       correct chunk at rank {matched_rank}/{k} · breadcrumb "
                  f"{'OK' if breadcrumb_ok else 'BROKEN'}")
            print(f"       [KB: {Path(citation['source']).name} · chunk {citation['chunk']}"
                  f" · {citation['date']} · score {citation['score']}]")
            snippet = citation["snippet"].replace("\n", " ")[:180]
            print(f"       {snippet}...")
        else:
            print(f"       no matching chunk in top-{k}; got:")
            for rank, hit in enumerate(hits, 1):
                source = hit["payload"].get("source_file", "?")
                print(f"         {rank}. {Path(str(source)).name}"
                      f" :: {(hit['payload'].get('text') or '')[:90]!r}")
        print()
    conn.close()
    print("S2 GATE:", "GREEN — surfaces may now be built on this corpus"
          if all_pass else "RED — nothing downstream moves (build doc §6)")
    return all_pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="blueleaf")
    args = parser.parse_args()
    return 0 if run_gate(args.workspace) else 1


if __name__ == "__main__":
    sys.exit(main())
