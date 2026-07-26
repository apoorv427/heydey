"""S1 retrieval-parity gate: heydey workspace store vs the proven legacy store.

For each golden-set query, embed ONCE, fetch top-5 point ids from BOTH stores
through the SAME search code path, score the overlap. Because the identical
query vector hits both sides, the score isolates copy fidelity + search parity.
Gate (build doc §6, S1): mean top-5 overlap >= 80%.

  .venv/bin/python -m heydey.parity_check --workspace blueleaf [--golden-set PATH]
"""

import argparse
import json
import sys

from . import config, workspaces
from .embedder import embed_texts
from .kb_migrate import default_source, open_source_readonly
from .vector_store import search_by_vector


def _default_golden() -> str | None:
    """Golden-set path is machine-local state -> corpus.json (`golden_set`)."""
    return config.load_corpus_config().get("golden_set")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-set", default=_default_golden())
    parser.add_argument("--source", default=default_source())
    parser.add_argument("--workspace", default="blueleaf")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    if not args.source or not args.golden_set:
        print("Missing paths: pass --source/--golden-set or set \"blos_source\" and "
              f"\"golden_set\" in {config.corpus_config_path()} (see INSTALL.md).",
              file=sys.stderr)
        return 2

    queries = json.load(open(args.golden_set))["queries"]
    src = open_source_readonly(args.source)
    dst = workspaces.connect(args.workspace)
    k = args.top_k

    overlaps = []
    for query in queries:
        vec = embed_texts([query])[0]
        src_ids = [h["point_id"] for h in search_by_vector(src, vec, limit=k)]
        dst_ids = [h["point_id"] for h in search_by_vector(dst, vec, limit=k)]
        overlap = len(set(src_ids) & set(dst_ids)) / k
        overlaps.append(overlap)
        flag = "" if overlap >= 0.8 else "  <-- low"
        print(f"  {overlap:.0%}  {query}{flag}")

    mean = sum(overlaps) / len(overlaps)
    passed = mean >= 0.80
    print(f"\nMean top-{k} overlap: {mean:.1%} over {len(queries)} queries — "
          f"{'PASS (>=80%)' if passed else 'FAIL (<80%)'}")
    src.close()
    dst.close()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
