"""B7 — the validator-independence experiment (W4, panel F8).

Question under test: today's validator judges claims against the SAME chunks
the executor wrote from (shared-input validation). If retrieval surfaced
misleading evidence, writer and checker fail TOGETHER — a correlated failure
no cross-family pairing can catch. The independent arm breaks the correlation:
for every claim-sentence the checker performs ITS OWN retrieval pass (the
claim itself is the query) and judges the claim against its own evidence.

Design notes, honest by construction:

- The SHARED arm is the production pipeline result itself (its receipts carry
  the per-sentence verdicts) — not a re-simulation.
- The INDEPENDENT arm reuses ``validator.validate`` per claim (same fail-closed
  parse, same family rule, same hedge handling) with ``ask.retrieve(claim)``
  as the evidence. Question-level re-retrieval would be a no-op — retrieval is
  deterministic, same query -> same chunks — so per-claim is the only pass
  that is actually independent.
- Artifacts (answers, corpus text) stay machine-local under
  ``HEYDEY_HOME/eval/``. What gets published in-repo is the AGGREGATE delta
  table only — counts and rates, never corpus content.

Claim-language rule (specs §5): the product claim stays
"vendor-conflict-free checking" until this measured delta says otherwise.

  .venv/bin/python -m heydey.validator_independence --workspace blueleaf --limit 50
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import ask, config, models_config, models_state, pipeline, validator, workspaces
from .pipeline import AgentSpec
from .s3_eval import _load_prompts


def independent_verdicts(conn, answer: str, *, executor_model: str,
                         validator_model: str, k_claim: int = 4,
                         retrieve_fn=None, complete_fn=None) -> list[dict]:
    """One verdict per claim-sentence, each judged against the checker's OWN
    per-claim retrieval. Returns [{sentence_index, claim, grounded, evidence_docs}]."""
    retrieve = retrieve_fn or ask.retrieve
    out: list[dict] = []
    for i, claim in enumerate(validator.split_claims(answer)):
        own_hits = retrieve(conn, claim, k=k_claim)
        vres = validator.validate(claim, own_hits, executor_model=executor_model,
                                  validator_model=validator_model,
                                  complete_fn=complete_fn)
        grounded = bool(vres.passed) if vres.passed is not None else False  # offline -> unproven
        docs = sorted({(h.get("payload", {}).get("doc_id")
                        or h.get("payload", {}).get("source_file") or "")
                       for h in own_hits} - {""})
        out.append({"sentence_index": i, "claim": claim, "grounded": grounded,
                    "evidence_docs": docs})
    return out


def _shared_verdicts(receipts: list[dict]) -> list[dict]:
    """The production (shared-input) arm, read straight off the run's receipts."""
    return [{"sentence_index": r["sentence_index"],
             "grounded": bool(r["validator_pass"]),
             "doc_id": r.get("doc_id") or ""} for r in receipts]


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return round(len(a & b) / max(1, len(a | b)), 3)


def aggregate(rows: list[dict]) -> dict:
    """The delta table: per-claim agreement matrix + per-prompt pass flips.
    Errored cases are counted, disclosed, and excluded from every rate."""
    errored = sum(1 for r in rows if r.get("answer_kind") == "error")
    rows = [r for r in rows if r.get("answer_kind") != "error"]
    cells = {"both_grounded": 0, "shared_only": 0, "independent_only": 0, "neither": 0}
    claims = 0
    flips = {"shared_pass_indep_fail": 0, "shared_fail_indep_pass": 0, "agree": 0}
    overlaps: list[float] = []
    for row in rows:
        shared_by_i = {v["sentence_index"]: v["grounded"] for v in row["shared"]}
        for iv in row["independent"]:
            s = shared_by_i.get(iv["sentence_index"])
            if s is None:
                continue  # receipts and split disagree on sentence count — skip, count nothing
            claims += 1
            key = ("both_grounded" if s and iv["grounded"] else
                   "shared_only" if s else
                   "independent_only" if iv["grounded"] else "neither")
            cells[key] += 1
        if row["pass_shared"] == row["pass_independent"]:
            flips["agree"] += 1
        elif row["pass_shared"]:
            flips["shared_pass_indep_fail"] += 1
        else:
            flips["shared_fail_indep_pass"] += 1
        overlaps.append(row["evidence_overlap"])
    return {
        "prompts": len(rows),
        "prompts_errored": errored,
        "claims_compared": claims,
        "agreement_cells": cells,
        "prompt_pass_flips": flips,
        "mean_evidence_overlap_jaccard":
            round(sum(overlaps) / len(overlaps), 3) if overlaps else None,
    }


def run(workspace_id: str, profile: str | None = None, limit: int | None = None,
        k_claim: int = 4) -> dict:
    profile = profile or models_state.active_profile()
    pair = models_config.get_pair(profile, "ask")
    conn = workspaces.connect(workspace_id)
    spec = AgentSpec(id="b7", name="B7 Analyst", task_class="ask", k=6)

    grounded, unanswerable, false_premise, injection = _load_prompts()
    cases = ([("grounded", q) for q in grounded]
             + [("unanswerable", q) for q in unanswerable]
             + [("false-premise", q) for q, _ in false_premise]
             + [("injection", q) for q, _ in injection])
    if limit:
        cases = cases[:limit]

    rows: list[dict] = []
    t0 = time.time()
    try:
        for n, (category, question) in enumerate(cases, 1):
            try:  # per-case isolation (Contract B): one provider hiccup never kills the run
                r = pipeline.run_pipeline(conn, spec, question, profile=profile,
                                          workspace_id=workspace_id)
                shared = _shared_verdicts(r.receipts)
                indep = independent_verdicts(
                    conn, r.answer, executor_model=pair.executor,
                    validator_model=pair.validator, k_claim=k_claim)
                shared_docs = {v["doc_id"] for v in shared if v["doc_id"]}
                indep_docs = {d for iv in indep for d in iv["evidence_docs"]}
                row = {
                    "n": n, "category": category, "question": question,
                    "answer_kind": r.answer_kind,
                    "pass_shared": all(v["grounded"] for v in shared) if shared else True,
                    "pass_independent": all(v["grounded"] for v in indep) if indep else True,
                    "shared": shared,
                    "independent": indep,
                    "evidence_overlap": _jaccard(shared_docs, indep_docs),
                }
                rows.append(row)
                print(f"[{n:2}/{len(cases)}] {category:14} shared={'P' if row['pass_shared'] else 'F'}"
                      f" indep={'P' if row['pass_independent'] else 'F'}"
                      f" overlap={row['evidence_overlap']:.2f}  {question[:40]}", flush=True)
            except Exception as exc:
                rows.append({"n": n, "category": category, "question": question,
                             "answer_kind": "error", "error": str(exc)[:200],
                             "pass_shared": None, "pass_independent": None,
                             "shared": [], "independent": [], "evidence_overlap": None})
                print(f"[{n:2}/{len(cases)}] {category:14} ERROR: {str(exc)[:80]}", flush=True)
    finally:
        conn.close()

    summary = {
        "experiment": "b7-validator-independence",
        "workspace": workspace_id, "profile": profile,
        "executor": pair.executor, "validator": pair.validator,
        "k_claim": k_claim,
        "wall_time_s": round(time.time() - t0, 1),
        **aggregate(rows),
        "rows": rows,
    }
    eval_dir = config.heydey_home() / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    out_path = eval_dir / "b7-validator-independence.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"\nartifact: {out_path}")
    return summary


def publish_table(summary: dict) -> str:
    """The in-repo publishable delta table — AGGREGATES ONLY, no corpus text."""
    c = summary["agreement_cells"]
    f = summary["prompt_pass_flips"]
    return "\n".join([
        "| Metric | Value |",
        "|---|---|",
        f"| Prompts | {summary['prompts']} |",
        f"| Prompts errored (excluded from rates, disclosed) | {summary.get('prompts_errored', 0)} |",
        f"| Claims compared | {summary['claims_compared']} |",
        f"| Both arms grounded | {c['both_grounded']} |",
        f"| Shared-input only (correlated-retrieval suspects) | {c['shared_only']} |",
        f"| Independent only | {c['independent_only']} |",
        f"| Neither | {c['neither']} |",
        f"| Prompt verdict agreement | {f['agree']}/{summary['prompts']} |",
        f"| Shared PASS -> independent FAIL | {f['shared_pass_indep_fail']} |",
        f"| Shared FAIL -> independent PASS | {f['shared_fail_indep_pass']} |",
        f"| Mean evidence overlap (Jaccard, docs) | {summary['mean_evidence_overlap_jaccard']} |",
        f"| Executor / validator | {summary['executor']} / {summary['validator']} |",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="blueleaf")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--k-claim", type=int, default=4)
    args = parser.parse_args()
    summary = run(args.workspace, profile=args.profile, limit=args.limit,
                  k_claim=args.k_claim)
    print("\n" + publish_table(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
