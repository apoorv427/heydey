# Known issues

Heydey publishes gates when they're green; this page is the other half of that
discipline — the gaps that aren't closed yet, with the measured number where one
exists and the gate that closes it. Nothing here is a surprise to us; it's what
we found when we measured, in the open, before anyone else asked.

| Issue | What's wrong | Measured | Closes when |
|---|---|---|---|
| **Knowledge graph** | Entity identity collisions, near-floor-confidence noise, and zero relationship edges — the graph panel is real, the data behind it isn't trustworthy yet. Root cause and fix in progress; full write-up: [GRAPH-TEARDOWN-2026-07-27.md](GRAPH-TEARDOWN-2026-07-27.md). | Doc coverage **3.4%** (43 of 1,271 docs — the migrated 35k-point KB never passed through the indexer) · precision **~18%** (3,319 of 4,268 entities sit exactly at the 0.55 confidence floor; function words and path fragments were nodes) · **95.8%** of entities are orphans · `relationships` table: **0 rows**. | Gated rebuild passes **all** of: ≥95% doc coverage, <15% orphans, zero duplicate canonical keys, ≥80% precision on 50 sampled edges, a 2-hop typed path returning receipts. |
| **Connectors** | The three shipped connectors (Google Workspace, Slack, Shopify shapes) are synthetic **demo** MCP servers that exercise the real security pipeline — they are not live third-party integrations. | OAuth + PKCE plumbing (consent → token exchange → refresh in the OS Keychain, RFC-7636 vector-tested) is built and mocked-endpoint tested; the live leg is credential-gated. | A non-founder completes real OAuth consent against live client credentials and a synced document lands in a receipt. Targeted August. |
| **Install** | Installing Heydey today still means opening a terminal: clone the repo, create a Python venv, pull two local models, run `heydey init`. There is no non-technical installer. | ~15–20 minutes end-to-end per [INSTALL.md](../INSTALL.md); founder-measured **2.9 minutes** to a cited, gate-checked answer once `heydey init` starts (that number is time-to-first-receipt, not install time). | No gate defined yet — a packaged, terminal-free installer is a named next step, not yet scheduled against a build slice. **Using** Heydey once installed needs no AI client (the web app and the Foundry's 5-question onboarding are the whole interface); this line is specifically about the one-time install step, and it is worth being upfront about the difference. |
| **Eval refusal rate** | The fail-closed validator means Heydey would rather say nothing than say something ungrounded — and today it refuses more than it answers, even on questions it could ground. | On the 50-prompt adversarial eval (2026-07-27 re-run): **42/50 (84%) refused overall**; within the 20 questions built to be answerable ("grounded" category), **12/20 (60%) still refused**. Zero ungrounded claims and zero fabrications on everything that *did* answer. | Not a bug on a gate — a fail-closed trade-off, tunable by executor strength: a separate cross-family swap experiment (DeepSeek writing, qwen3 checking) moved substantive grounded answers from 8/20 to 14/20 with fabrications held at zero (see README "Next"). |
| **Reads are unauthenticated on localhost** | The supervisor's read endpoints (GET/HEAD/OPTIONS) pass without the per-launch bearer token — only state-changing requests (POST/PUT/DELETE/PATCH) require the token plus an allowed Origin header. The `127.0.0.1`-only bind is the sole boundary for reads today. | Design choice, not a defect: single-user, single-Mac deployment model, documented in `api/heydey/auth.py`. | Deliberate for now; revisit when Heydey exposes itself as a remotely-reachable MCP server — the moment more than "only you, on this Mac" can reach the port, reads need the same gate writes already have. |

## Rebuild progress — measured 2026-07-27 evening (deterministic pass complete)

The deterministic rebuild ran over the full production corpus in **13.4 s** (1,271 docs,
0 failures), with **no re-embedding** — the existing vectors were untouched.

| Rebuild gate | Target | Measured now | |
|---|---|---|---|
| Duplicate canonical keys | 0 | **0** | ✅ |
| Edges missing provenance | 0 | **0** of 19,942 edges | ✅ |
| Entity identity (worst case) | 1 node per thing | **1** node, 107 mentions (was 26) | ✅ |
| Junk in the top-ranked nodes | none | top nodes are real products/orgs/dates; the money-fragment and marker hubs are gone | ✅ |
| Doc coverage | ≥95% | **85.7%** (1,089 of 1,271) | ❌ |
| Orphan rate | <15% | **72.7%** | ❌ |

**Why the last two are still open, stated plainly:** the deterministic pass only draws
co-mention edges among the highest-confidence entities in each document, so entities
beyond that cut are recorded with provenance but not yet connected. The typed-relation
pass (a local model, anti-hallucination checked, ~6 h over the corpus) is what closes
them. **Until both gates are green, this rebuild is "in progress", not "done"** — and
nothing in this repo or our application claims otherwise.
