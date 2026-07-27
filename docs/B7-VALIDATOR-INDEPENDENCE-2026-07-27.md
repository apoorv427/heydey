# Validator-independence experiment — measured delta (2026-07-27)

**Question.** Heydey's production validator judges every claim-sentence against the
same retrieved chunks the executor wrote from (*shared-input* validation). If
retrieval surfaces misleading or stale evidence, writer and checker can fail
together — a correlated failure that cross-family pairing alone cannot catch.
This experiment measures that blind spot: an *independent* arm re-checks every
claim against the checker's **own retrieval pass** (the claim itself is the
query), then we compare verdicts.

Method notes, honest by construction: the shared arm is the production
pipeline's own receipts (not a re-simulation); the independent arm reuses the
same fail-closed validator path with per-claim retrieval (question-level
re-retrieval would be a no-op — retrieval is deterministic, same query → same
chunks). Runner: `api/heydey/validator_independence.py`. Raw artifacts stay
machine-local; this table is aggregates only.

## The numbers, straight

| Metric | Value |
|---|---|
| Prompts (4 adversarial classes) | 50 |
| Prompts errored (excluded, disclosed) | 0 |
| Claims compared | 86 |
| Both arms grounded | 82 |
| Shared-input only — correlated-retrieval suspects | **4 (4.7%)** |
| Independent only | 0 |
| Neither | 0 |
| Prompt verdict agreement | 47/50 |
| Shared PASS → independent FAIL | 3 |
| Shared FAIL → independent PASS | **0** |
| Mean evidence overlap (Jaccard, docs) | 0.109 |
| Executor / validator | deepseek/deepseek-chat / qwen3:8b |
| Wall time / added cost | 30.6 min / ~$0.04 |

The 0.109 evidence overlap says the checker genuinely examined different
evidence — this was independence, not an echo.

## Reading the 4 disagreements (spot-read adjudication, class-level)

- **1× independent-arm retrieval miss** — the claim is supported in the corpus,
  but claim-as-query surfaced adjacent research material instead of the
  supporting document. A false alarm of the independent arm.
- **1× compound-claim granularity artifact** — a long multi-element claim whose
  elements span several chunks; the independent arm retrieved the right source
  but the strict per-sentence judge would not pass the whole compound.
- **2× real catches** — the writer blended configuration naming from an older
  era of the corpus into a present-tense answer. Shared-input validation passed
  it *because the stale chunk sat in the shared context*; the independent pass,
  retrieving current documents, refused to confirm. This is exactly the
  correlated-evidence failure class the experiment was designed to expose.

## What changes and what doesn't

- **Claim language: unchanged.** The product claim remains
  "vendor-conflict-free checking" — this measurement does not widen it. What it
  adds: the shared-input blind spot is real and small (≈4.7% of claims, ~half of
  those adjudicated as true catches on spot-read), and the independent pass
  **never rescued a failure** (0 shared-FAIL → independent-PASS) — it only ever
  tightened.
- **Hardening candidate, not a default:** independent per-claim retrieval is a
  credible second pass for a future *strict* profile (cost: one extra
  small-model call per claim, local-lane compatible). It goes to the roadmap,
  gated like everything else.
- **Arm choice, disclosed:** run on the balanced profile (cloud executor →
  local checker) deliberately — the local-only executor refuses 42/50 of these
  prompts (measured the same morning), which leaves almost no claims to
  compare. The refusal-heavy honesty of local-only is documented separately in
  the repo's eval history.

*Runner + tests shipped in-repo; per-case isolation means one provider error
can never silently truncate a future run (it is counted and disclosed instead).*
