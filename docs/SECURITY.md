# SECURITY.md

**Heydey — the security posture, top to bottom, with the file/line that enforces
each claim.** Every rule is a *runtime enforcement* (a test failure or a runtime
raise), not a policy note. This doc is one of the S7 done-gate deliverables.

Last verified: 2026-07-27 · 225 tests green · all 11 gate runners PASS
(S2 · S3-artifacts · S4a/b/c · S5-core · S6a/b/c · S7 · parity).

## 1. The single sentence
Heydey answers only from your data, always cites its sources, and its writer-model
is never its checker-model. Every rule below exists to keep that sentence true
under adversarial input.

## 2. Structural isolation (§14-A1, `workspaces.py`)
- **One SQLite file per workspace** (`workspaces.create_workspace` → its own
  `heydey.db` under `~/.heydey/workspaces/<id>/`). The file boundary IS the
  isolation mechanism — Heydey never uses a `workspace_id` filter as a safety
  guarantee. A crossed connection returns None or nothing, never someone else's
  row.
- **`ATTACH DATABASE` denied at the sqlite3 authorizer** (`workspaces._authorizer`),
  so even a hypothetical SQL-injection path cannot cross workspace files.
- **Migrate-on-open** (`workspaces._open`) — schema bumps heal old workspaces on
  first touch, so the file-boundary guarantee doesn't drift with schema evolution.
- *Proof:* `tests/test_isolation.py`, `test_schema.py::test_old_schema_workspace_migrates_on_open`.
  S6c gate step 6 additionally verifies BOTH directions (client can't see
  blueleaf; blueleaf carries zero client chunks).

## 3. Authenticated localhost (S0, `auth.py` + `heydey_supervisor.py`)
- **Binds `127.0.0.1` only**, never `0.0.0.0`.
- **Per-launch bearer token** in `~/.heydey/runtime/supervisor.json` (chmod 0600),
  regenerated on every process start.
- **Mutations gated**: every POST/PUT requires `Bearer <token>` — the middleware
  refuses before any route runs.
- **Duplicate-launch guard**: `_already_running` probes `/health` before touching
  the runtime file, so a second start can never clobber the live token (fix
  landed after the token-clobber trap hit twice on 2026-07-19).
- *Proof:* `tests/test_auth.py`, `tests/test_secrets.py::test_no_secret_in_response`
  (sweeps every route, incl. 401 paths).

## 4. Secrets in Keychain, never in the DB (`secrets_store.py`)
- **Keychain first**, then a chmod-0600 env file fallback (`_from_env_file`);
  a group- or world-readable env file is *refused*.
- **Every served value is registered for log redaction** — a leaked log line
  gets `••• redacted •••` substituted before write.
- **No secret ever reaches `heydey.db`.** Connector credentials are named
  `heydey.{workspace_id}.{connector_id}` — per-item, no wildcard retrieval; the
  connectors table stores only `keychain_ref`.
- *Proof:* `test_secrets.py::test_log_redaction_masks_served_secrets`,
  `test_group_readable_env_file_refused`, `test_mcp_host.py::test_cred_isolation_across_workspaces`.

## 5. Cross-model validator, fail-closed (Executor Contract A · `validator.py`)
- **Executor family ≠ validator family** — enforced at CONFIG-WRITE
  (`models_config.save_profile` calls `Pair.check()`; a family-rule violation
  never touches disk) AND at RUNTIME (`validator.validate` re-checks).
- **Version-suffix immunity**: `llama3.1:8b` and `llama3.2:3b` normalise to the
  same family (regression after S4a caught the fail-open).
- **Fail-closed on unparseable verdicts**: an ambiguous validator reply is
  treated as FAIL, not a silent pass.
- **Retry-then-degrade**: a failing sentence retries once with the failed
  claims fed back; still failing → degrade to verbatim extractive, labeled
  `validator-degraded` — the unvalidated synthesized text NEVER ships.
- **Offline path**: no validator reachable → `qwen3:8b` (Ollama) is the
  documented fallback (Contract A #4). If no local validator either, the
  answer carries an explicit `UNVALIDATED — offline` badge — the one
  permitted bypass, always labeled.
- *Proof:* `test_validator.py` (family @save · family @task-override · shipped
  profiles valid · version-suffix immunity) · S3 gate (50 adversarial prompts:
  0 ungrounded, 0 fabrications).

## 6. Injection guard (Executor Contract C Layer 1 · `ingest_guard.py` + `ask.py`)
- **Every connector result passes `guard_mcp_result`** before any LLM context —
  flagged content is stored + tagged in `mcp_results` for audit, and the LLM
  sees `[connector_result_stored:<id>]` + a neutralized summary, NEVER raw
  connector text.
- **Retrieval-side exclusion**: `ask.retrieve` DROPS any chunk whose payload
  carries `injection_risk` (found live 2026-07-19: 4 flagged ops chunks were
  live-retrievable in blueleaf; the sync guard was safe only incidentally, no
  chunk-level filter existed until the S6b review). Every answer path (ask,
  brief, playbook) shares this one chokepoint.
- **Regression pinned**: `test_ask.py::test_flagged_chunk_never_retrieved`
  injects a flagged chunk and proves it can't be retrieved; s6b_gate[3]
  strengthened to do the same.

## 7. Outbound approval (Layer 2 · `mcp_host.py`)
- **Approval class decided at MANIFEST-PARSE**: pull/read tools = `none`;
  write/send/spend = `outbound`/`spend`; unknown verbs = `outbound` (fail-closed).
- **`call_tool` refuses** any tool with `approval_class != "none"` unless an
  APPROVED tray row id is presented — enforced in the HOST, so no surface can
  forget it.
- **The MVP write path is "prepared-action"** (L34 · `approvals.py`): approve →
  writes a CSV artifact + formats the exact admin deep-link + logs a receipt.
  Structurally imports no network library — `test_approvals.py` does an AST
  scan proving `approvals.py` cannot speak IP.
- *Proof:* `test_mcp_host.py::test_write_requires_tray`, `test_pull_tool_runs_free`,
  `test_approvals.py::test_approvals_module_imports_no_http_client`.

## 8. Regulatory egress (Contract C Layer 3 · S7 · `connectors.egress_allows` + `pipeline.run_pipeline`)
- **`Profile.egress` is the ONE switch**, set at profile save (`"any"` |
  `"local_only"`). Under `local_only`, any run whose retrieved corpus includes
  connector-sourced chunks refuses a cloud executor and degrades to verbatim
  extractive, labeled `egress-blocked`.
- **The check runs INSIDE `run_pipeline`**, not per-surface — so ask, brief,
  playbook, and Foundry-instantiated agents all inherit the same guarantee.
  Ollama-lane models pass; cloud models raise `EgressError`.
- **No prompt-hack**: the guard fires on `payload.source_type == "connector"`,
  a structural label, not model behavior.
- *Proof:* `test_pipeline.py::test_s7_egress_blocks_cloud_when_connector_content_and_local_only`
  (traps `llm_client.complete` to prove no cloud call was made under the guard)
  + `test_egress_ok_when_no_connector_content` (clean ops content still uses
  the cloud executor — the guard is about regulated data, not the model).

## 9. Workspace isolation for connector credentials (Layer 4 · `connectors.py`)
- Per-item Keychain naming (`heydey.{workspace}.{connector}`), no wildcards.
- Every accessor takes `workspace_id` and validates it against the row — a
  crossed call returns None.
- *Proof:* `test_mcp_host.py::test_cred_isolation_across_workspaces`.

## 10. PII scrub at every store point (`ingest_guard.py` + `episodic.py`)
- Indian phone / Aadhaar / email patterns → `[REDACTED-PII]` at chunk-store
  time; the *original* PII is written only to a chmod-0600 audit log at
  `~/.heydey/logs/pii-audit.jsonl` (never the corpus).
- The Session Browser (S5) scrubs the run's `intent` field on write — a phone
  number typed into Ask never reaches the Sessions surface.
- *Proof:* `test_ingest_guard.py::test_scrub_pii_indian_context`,
  `test_sentinel_episodic.py::test_record_run_scrubs_pii_at_write`.

## 11. Personal hard-wall (`ops_ingest.py::_excluded`)
- The `Personal/` folder and any file whose name contains `personal` (case-
  insensitive) is *refused* at the ingest layer — never enters points.
- The `/reveal` endpoint (Finder deep-link) enforces the same wall on
  outgoing reveals — the Personal wall runs in reverse too.
- *Proof:* `test_ops_ingest.py`, `test_server_s4a.py::test_reveal_walls`.

## 12. Anti-LightRAG / anti-cron doctrine (Executor Contract B · `graph.py`,
`morning_brief.py`)
- **No LLM inside any scheduled batch, ever.** The graph grows as a byproduct
  of queries; the Morning Brief runs deterministic extract/measure sections
  only (a test literally traps `llm_client.complete` during a brief run to
  prove it).
- **Per-doc/per-section error isolation** — one failing doc/section logs and
  continues; the pipeline never aborts.
- **Sentinel health-checks** the graph, retrieval, validator pass-rate, cost
  vs budget, and Morning Brief freshness — flags land in the next brief.
- *Proof:* `test_morning_brief.py::test_scheduled_path_makes_no_llm_call`,
  `test_graph.py::test_reingest_preserves_entity_ids_and_edges` (the
  identity-preserving fix — a Contract B structural bug that made 1,344
  edges dangle on re-ingest; caught + fixed at S4c).

## 13. Cite-or-silent / done-is-proven
- Every answer carries **per-sentence receipts** in `receipts` (source · chunk ·
  score · validator pass/fail · model · cost) — enforced in the pipeline, never
  the prompt.
- **Every slice has a gate that runs**: S2 · S3 · S4a · S4b · S4c · S5-core ·
  S6a · S6b · S6c. Every green is a runnable command, not a claim.
- **Wall-clock honesty rule** (S6c D4a): the storyboard's `9:41` placeholder is
  replaced by measured `foundry_events` timestamps in every real capture.

## 14. What's out of scope (Phase-2, deliberately)
- **Real outbound connector writes** — MVP is prepared-action only; every
  connector "write" today is a local artifact + admin deep-link + receipt.
- **Multi-user / RBAC / SQLCipher at rest** — single-user local deployment;
  documented as a Phase-2 delta in this doc, not built.
- **A `remove workspace` API** — an isolation test may `rmtree` its own
  synthetic `s6c-*` dir; the product surface has no delete (this doc is the
  Phase-2 marker for it).
- **Full DPDP consent ledger** — the shipped switch (`Profile.egress`) is the
  clinic/hospital posture. The consent + purpose-limitation ledger is a
  Phase-2 delta, called out here so nobody backfills it as scope creep.

## 15. The Personal hard-wall (operational)
Anything under a `Personal/` directory — wherever it lives on the operator's
machine — is OFF LIMITS to Heydey and to any agent operating on this repo:
nothing from it may enter any corpus, workspace, graph, log, or status surface.
The structural wall in `ops_ingest.py::_excluded` enforces this at ingest;
installation-specific deny-list names live in the machine-local
`~/.heydey/corpus.json`, never in source.

---

*Heydey security doc · S7 · 2026-07-21. Every claim above is enforced at
runtime, not prompt-time. If a claim in this doc feels aspirational, that is a
bug in this doc.*
