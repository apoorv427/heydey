"""B7 — validator-independence experiment harness (W4).

The math and the independence mechanics under test with injected retrieval +
completion; the real run happens offline against the live corpus and publishes
aggregates only."""

import json

from heydey import validator_independence as b7


def _hit(doc, text, score=0.9):
    return {"payload": {"doc_id": doc, "text": text}, "vscore": score}


def test_independent_arm_retrieves_per_claim():
    """The checker's own pass: one retrieval PER CLAIM, claim text as query."""
    queries = []

    def retrieve_fn(conn, query, k):
        queries.append(query)
        return [_hit("doc-a", "The floor is 25 lakh.")]

    def complete_fn(model, prompt, system, max_tokens):
        return json.dumps([{"i": 0, "grounded": True, "chunk": 1, "reason": "stated"}])

    verdicts = b7.independent_verdicts(
        None, "The floor is 25 lakh. The deal closes in August.",
        executor_model="llama3.1:8b", validator_model="qwen3:8b",
        retrieve_fn=retrieve_fn, complete_fn=complete_fn)

    assert len(verdicts) == 2
    assert queries == ["The floor is 25 lakh.", "The deal closes in August."]
    assert all(v["evidence_docs"] == ["doc-a"] for v in verdicts)
    assert all(v["grounded"] for v in verdicts)


def test_independent_arm_fails_closed_on_unparseable_verdict():
    def complete_fn(model, prompt, system, max_tokens):
        return "not json at all"

    verdicts = b7.independent_verdicts(
        None, "A specific fact claim.",
        executor_model="llama3.1:8b", validator_model="qwen3:8b",
        retrieve_fn=lambda conn, q, k: [_hit("d", "x")], complete_fn=complete_fn)
    assert verdicts[0]["grounded"] is False  # unproven, never a silent pass


def test_aggregate_agreement_matrix_and_flips():
    rows = [
        {  # full agreement, same evidence
            "shared": [{"sentence_index": 0, "grounded": True, "doc_id": "a"}],
            "independent": [{"sentence_index": 0, "grounded": True, "evidence_docs": ["a"]}],
            "pass_shared": True, "pass_independent": True, "evidence_overlap": 1.0,
        },
        {  # the correlated-retrieval suspect: shared said grounded, independent refutes
            "shared": [{"sentence_index": 0, "grounded": True, "doc_id": "a"},
                       {"sentence_index": 1, "grounded": False, "doc_id": ""}],
            "independent": [
                {"sentence_index": 0, "grounded": False, "evidence_docs": ["b"]},
                {"sentence_index": 1, "grounded": False, "evidence_docs": []}],
            "pass_shared": True, "pass_independent": False, "evidence_overlap": 0.0,
        },
    ]
    agg = b7.aggregate(rows)
    assert agg["claims_compared"] == 3
    assert agg["agreement_cells"] == {"both_grounded": 1, "shared_only": 1,
                                      "independent_only": 0, "neither": 1}
    assert agg["prompt_pass_flips"] == {"shared_pass_indep_fail": 1,
                                        "shared_fail_indep_pass": 0, "agree": 1}
    assert agg["mean_evidence_overlap_jaccard"] == 0.5


def test_publish_table_is_aggregates_only():
    summary = {
        "prompts": 2, "claims_compared": 3,
        "agreement_cells": {"both_grounded": 1, "shared_only": 1,
                            "independent_only": 0, "neither": 1},
        "prompt_pass_flips": {"shared_pass_indep_fail": 1,
                              "shared_fail_indep_pass": 0, "agree": 1},
        "mean_evidence_overlap_jaccard": 0.5,
        "executor": "llama3.1:8b", "validator": "qwen3:8b",
        "rows": [{"question": "SECRET business question?",
                  "shared": [], "independent": []}],
    }
    table = b7.publish_table(summary)
    assert "Claims compared | 3" in table
    assert "SECRET" not in table  # corpus/question text never reaches the repo
    assert "llama3.1:8b / qwen3:8b" in table


def test_aggregate_discloses_and_excludes_errored_cases():
    rows = [
        {"shared": [{"sentence_index": 0, "grounded": True, "doc_id": "a"}],
         "independent": [{"sentence_index": 0, "grounded": True, "evidence_docs": ["a"]}],
         "pass_shared": True, "pass_independent": True, "evidence_overlap": 1.0},
        {"answer_kind": "error", "error": "openrouter error: rate limit",
         "shared": [], "independent": [], "pass_shared": None,
         "pass_independent": None, "evidence_overlap": None},
    ]
    agg = b7.aggregate(rows)
    assert agg["prompts"] == 1 and agg["prompts_errored"] == 1
    assert agg["prompt_pass_flips"]["agree"] == 1  # the None==None row never counted
    assert agg["mean_evidence_overlap_jaccard"] == 1.0


def test_no_claims_means_trivial_agreement():
    verdicts = b7.independent_verdicts(
        None, "", executor_model="llama3.1:8b", validator_model="qwen3:8b",
        retrieve_fn=lambda conn, q, k: [], complete_fn=lambda *a: "[]")
    assert verdicts == []
