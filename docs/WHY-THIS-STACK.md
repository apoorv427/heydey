# Why this stack

Heydey's stack gets questioned from two directions — "why not do everything in
JavaScript/React?" and "why hand-build instead of using agent frameworks?" This
memo answers both. Every choice below is enforced by tests, not preference.

## The shape

```
UI          React 19 + Next.js 15 + D3        (webapp/ — everything the user sees)
Backend     Python 3.12 + FastAPI              (api/ — supervisor, 127.0.0.1 only)
AI core     fastembed (bge-small, 384-dim) · sqlite-vec · FTS5 · Ollama (local)
            + thin raw-API clients for cloud lanes (no SDK frameworks)
Storage     SQLite — ONE file per workspace
Transport   MCP (protocol only) for tool connectors
```

## The three load-bearing decisions

### 1. SQLite, one file per workspace — never a database server

Workspace isolation is **structural**: a connection opened for workspace A
physically cannot return workspace B's rows, because B is a different file
(`ATTACH` is denied by a SQLite authorizer, and there is a test for that). A
server database — Postgres, MongoDB, any hosted vector DB — would turn that
guarantee back into a `WHERE workspace_id = ?` filter, i.e., a bug away from a
data leak. It would also break the product's core promises:

- **Local-first / data sovereignty:** an embedded file has no daemon, no port,
  no network surface. For regulated buyers (India's DPDP), "your data is one
  file on your disk, and the pipeline runs with the network off" is the entire
  sales conversation.
- **Verifiability:** receipts point into the same file the answers came from.
- **Zero-ops:** no service to install, misconfigure, or keep patched.

sqlite-vec + FTS5 give vector + keyword search inside that same file — a
credible, well-documented pattern, stated honestly: sqlite-vec is pre-1.0, so
we pin the version and keep the vector store behind a frozen ~230-LOC
interface; a storage-format change before 1.0 is a migration we control, not a
surprise. It is fast enough: the full test suite over this stack runs in ~2.5
seconds.

### 2. Python for the AI core — because the *hard* layer's ecosystem is Python

Precision about what Python buys: embeddings and retrieval *could* be built in
JS today (AnythingLLM ships a pure-JS pipeline; Ollama itself is Go, with
first-party Python and JS clients alike). What has no JS equivalent of
comparable depth is the layer Heydey actually competes on — eval harnesses,
statistical tooling, and the PII/injection-guard lineage that makes the
validator gate *proof-grade* — which lives in Python's scientific stack. The
backend is ~11k LOC of Python with 223 passing tests and 11 falsifiable gate
runners. A JavaScript backend is *possible* in 2026: for the retrieval layer
it's a lateral move, for the verification layer it's a downgrade — and
rewriting a verified system either way is how proof gets destroyed.

The predecessor system this replaced also used a React frontend with a Python
API — plus Docker, Postgres+pgvector, Redis, Celery, MinIO, LangGraph, and a
cloud embedding dependency, with zero tests. It failed on discipline, not
language. Heydey's rebuild kept the language split and removed the services:
the banned-dependency list (langchain/langgraph/crewai/autogen, Celery, Redis,
MinIO, Postgres, Docker, hosted vector DBs, Neo4j) is enforced by a test that
greps both the Python imports and the webapp's package.json on every run.

### 3. React where React is the right tool — the entire product surface

Everything the user sees is React: 10 surfaces (Ask, Today, Library, Graph,
Models, Agents, Connectors, Sessions, Status), D3 for the activity graph,
Next.js API routes as a server-side proxy so the supervisor's bearer token
never reaches the browser. "Should this be built in React?" — it is.

## Why not "everything in JavaScript" (MERN / Electron-style)?

- **M**ongoDB: see decision #1 — a database server is the one thing this
  product must never have.
- **E**xpress/**N**ode: see decision #2 — the AI core's ecosystem is Python;
  the HTTP layer is a thin FastAPI app either way.
- **R**eact: already the entire UI.

An all-JS desktop shell (Electron/Tauri) is a *packaging* question, not a
stack question — see below.

## Why no agent frameworks?

The product's guarantee — every answer carries a receipt (source · chunk ·
score · cross-model validator pass/fail · cost), fail-closed — has to be
enforced *in the pipeline*. Frameworks own the pipeline; owning the guarantee
means owning the loop. The agent runtime, retrieval engine, validator gate,
MCP host, and cost telemetry are hand-built (~7.4k LOC) and individually
tested. MCP is used as a *protocol* (transport), not a framework.

## The honest critique, and the roadmap answer

The legitimate challenge to this stack isn't language — it's **distribution**.
Today, installing Heydey means cloning a repo, creating a Python venv, and
running `npm install` (see INSTALL.md). That's fine for design partners; a
consumer-grade product wants one signed `.app`. The plan is a thin native shell
(Tauri or a Swift menu-bar app) over the same Python core once external-user
retention justifies it. The core doesn't change — Jan's own Electron→Tauri
migration never touched its C++ engine — but packaging is a real project in its
own right (months, not days), which is exactly why it's sequenced after
retention evidence rather than before. Until then, the venv install is the
honest state, documented as such.

## What would change these decisions

- A **team/cloud sync** offering would add a server component (and possibly a
  TS service) *alongside* local workspaces — additive, not a migration.
- The JS ecosystem already covers embeddings/retrieval respectably; if it ever
  matched Python's eval/statistical/guard tooling — the verification layer —
  decision #2 would be revisited. As of mid-2026 it does not.
- Nothing revisits decision #1. One file per workspace is the product.

---

*Adversarially fact-checked against shipping local-first products (2026-07-26);
overstated claims were corrected rather than defended. House style.*
