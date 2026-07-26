# Heydey — Build Context (auto-loaded)

**You are working on Heydey: a local-first, proof-grade AI operating system — "does the work, shows its receipts."**

## The non-negotiables
- **Retrieval is the hard gate.** The S2 gate (documented failure queries → correct chunk top-3 with a working breadcrumb) must be green before any surface, connector, or graph feature ships on a corpus. Do not build UI on unproven retrieval.
- **Wrapper-free.** The agent runtime, retrieval engine, validator gate, supervisor, artifact engine, Foundry, MCP host, and cost telemetry are hand-built. **BANNED (CI-tested):** langchain / langgraph / crewai / autogen, Celery / Redis / MinIO / Postgres, hosted vector DBs, Docker, Neo4j / networkx. **Allowed rails:** Python 3.12, SQLite (+sqlite-vec/FTS5), Ollama/MLX, raw model APIs (thin clients), Next.js/React, D3 (UI-only), the MCP protocol.
- **One SQLite file per workspace.** Isolation is structural — never a `workspace_id` filter.
- **Cite-or-silent / done = proven.** Every answer and action carries a receipt (source · chunk · score · validator · model · cost). Cross-model validator (executor family ≠ validator family), fail-closed. No silent failures.
- **Every surface ships 4 states** (loaded / empty-with-CTA / error-with-next-step / ingesting). A blank panel is a defect.
- **`Personal/` and anything under it is OFF LIMITS.** Machine-local values (corpus sources, deny names, gate queries, eval prompts) live in `~/.heydey/corpus.json` and `~/.heydey/eval_prompts.json`, never in source.

## Agentic build discipline (Karpathy-derived 4-rule set, adapted to the gate culture)
1. **Think before coding.** State assumptions and trade-offs BEFORE the first edit; surface ambiguity — don't silently pick.
2. **Simplicity first.** The 100-line version beats the 1000-line version until a gate proves otherwise. No new abstraction without a second caller. Prefer deleting code.
3. **Surgical changes.** Touch only what the task requires; never "improve" orthogonal code as a side effect; match the surrounding idiom.
4. **Goal-driven execution.** Write or name the falsifiable gate before building toward it; "done" is the gate passing, never the code existing.

## Verify state, don't assume
`git log`, run the tests (`api/.venv/bin/python -m pytest api/tests -o addopts=""`), drive the actual flow. The gate runners (`python -m heydey.s2_gate` … `s7_gate`) are the definition of done — satisfy them literally.
