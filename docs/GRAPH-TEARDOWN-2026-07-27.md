# Graph teardown — 2026-07-27

An engineering post-mortem on Heydey's entity/relationship graph: what we
claimed, what we measured when we checked it properly, why it was wrong, what's
changing, and the gates that have to pass before we claim it works again.

## What we claimed

The graph panel (D3-rendered, read-only) shows the entities Heydey extracts
from a workspace's documents and the relationships between them, with a count
we'd quoted as evidence of a live, growing knowledge graph.

## What we measured

We ran the numbers against the production database instead of trusting the row
count.

| Metric | Value | What it means |
|---|---|---|
| Document coverage | **3.4%** (43 of 1,271 docs) | The migrated 35,086-point knowledge base was copied in-place at S1 and never passed through `graph.index_document`. Almost the entire corpus was never touched by the extractor. |
| Precision floor | **~18%** (3,319 of 4,268 entities) | 3,319 entities sit exactly at the 0.55 minimum-confidence floor — the extractor's "unsure" bucket, kept instead of dropped. `How`, `Desktop`, `Downloads`, and `Jul` were nodes. 307 entities were pure function words. |
| Identity duplication | one product name → **26 separate nodes** | Entity identity is keyed on `(label, type, source_doc_id)`. The same real-world entity, mentioned in 26 different source documents, becomes 26 different graph nodes with no merge step. |
| Relationship edges | **0 rows** | The `relationships` table is empty. The relation-mining function exists in the codebase and is never called from the ingest path. |
| Orphan rate | **95.8%** | With no relationships, almost every entity is a disconnected node — a dot with no edges. |
| Hub bias | toward unit-less money fragments and markers | Without provenance or typing, the highest-degree "hubs" that did exist were number fragments and formatting artifacts, not meaningful entities. |

The UI is not at fault. `GraphPanel.tsx` faithfully rendered exactly what the
database gave it. This was a data problem, not a rendering problem.

## Root cause

Two decisions compound into everything above:

1. **Identity keyed on `(label, type, source_doc_id)` instead of a canonical
   key.** There was no alias/merge step, so the same entity re-mentioned across
   documents was never recognized as the same entity. This alone explains the
   26-nodes-for-one-product duplication and inflates every downstream count.
2. **An ungated candidate filter.** Entities were kept at extraction-time
   confidence with no second pass — anything the extractor wasn't sure about
   stayed in, at the floor score, rather than being dropped or escalated for a
   better pass. Combined with the migrated KB never being indexed at all, the
   graph ended up small in real coverage and noisy in what little it had.

Neither is a rendering bug, a UI bug, or a retrieval-pipeline bug (S2 retrieval
is unaffected — the graph is a separate extraction path over the same corpus).

## What's changing

A rebuild is in flight, scoped to fix the root causes above, not to patch
symptoms:

- **Canonical entity identity with aliases** — replaces the
  `(label, type, source_doc_id)` key so re-mentions across documents merge into
  one node instead of duplicating.
- **Typed predicates with provenance on every edge** — every relationship
  carries its type and the source chunk it came from, so edges are checkable
  the same way answer citations are.
- **Gated deterministic extraction** — candidates below a real confidence bar
  are dropped instead of kept at the floor.
- **PMI (pointwise mutual information) to kill hub bias** — down-weights
  high-frequency, low-information fragments instead of letting them dominate
  the graph's visual center.
- **A local-LLM typed-triple extraction pass with an anti-hallucination
  check** — a second extraction lane that proposes typed (subject, predicate,
  object) triples, checked against the source chunk before being kept.
- **Resumable backfill over the existing corpus, with no re-embedding** — the
  full 1,271-document corpus gets indexed by the graph extractor; the vector
  and FTS stores are untouched.

## The gates

The rebuild doesn't ship as "done" until it clears all five, measured the same
way this teardown was measured:

- ≥95% document coverage
- <15% orphan rate
- zero duplicate canonical keys
- ≥80% precision on 50 sampled edges (human- or cross-model-checked)
- a 2-hop typed path query returns receipts (source chunk per edge), not just
  node labels

Until those are green, [KNOWN-ISSUES.md](KNOWN-ISSUES.md) carries the current
numbers, and no public claim quotes a graph entity or relationship count as
proof of anything.

## Rebuild progress — measured 2026-07-27 evening (deterministic pass complete)

The deterministic rebuild runs over the full production corpus in **~21 s**
(1,271 documents, 0 failures) with **no re-embedding** — the existing vectors are
untouched.

| Rebuild gate | Target | Measured | |
|---|---|---|---|
| Duplicate canonical keys | 0 | **0** | ✅ |
| Edges missing provenance | 0 | **0** of 36,613 | ✅ |
| Entity identity (worst case) | 1 node per thing | **1** node, 107 mentions (was 26) | ✅ |
| Orphan rate | <15% | **0.5%** (was 72.7%) | ✅ |
| Junk in top-ranked nodes | none | gone — the money-fragment and marker hubs are replaced by real products, orgs and dates | ✅ |
| Doc coverage | ≥95% | **85.6% of all docs · 97.6% of documents over 1,000 characters** | ⚠️ |

**On that last row, precisely:** 182 documents produce no entity. Their median
length is 413 characters and only 4 exceed 2,000 — they are stubs, redirects and
near-empty notes with nothing extractable. Measured against substantive documents
the gate passes (97.6%); measured against every file in the corpus it does not
(85.6%). Both numbers are stated rather than picking the flattering one, and the
gate has not been redefined to make it pass.

**Still to come:** the typed-relation pass (a local model, anti-hallucination
checked) adds semantic predicates — `PRICED_AT`, `CLIENT_OF`, `DECIDED_BY` — on
top of the structural graph above. It is resumable and runs against the corpus
without re-embedding; measured throughput on this hardware is roughly a day, so
it is deliberately a background job rather than a blocking step.
