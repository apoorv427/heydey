# Installing Heydey

Heydey is local-first: everything below runs on your machine, and the default
`local-only` profile makes **zero network calls** at answer time.

**Requirements:** macOS (Apple Silicon recommended) · Python 3.12 · Node 18+ ·
[Ollama](https://ollama.com)

## 1. Clone + Python environment

```bash
git clone <this-repo> heydey && cd heydey
python3.12 -m venv api/.venv
api/.venv/bin/pip install -e "api[dev]"
```

## 2. Local models

The default profiles use one executor and one validator from **different model
families** (the proof-grade rule — enforced in code):

```bash
ollama pull llama3.1:8b   # executor (writes answers)
ollama pull qwen3:8b      # validator (checks them — different family, fail-closed)
```

The embedding model (`BAAI/bge-small-en-v1.5`, 384-dim) downloads automatically
via fastembed on first ingest.

## 3. One command: `heydey init`

```bash
ln -s "$PWD/heydey" /usr/local/bin/heydey   # or ./heydey from the repo root
heydey init --workspace mycompany --root ~/Documents/company-docs
```

Writes the config, creates the workspace, ingests, runs the retrieval gate, and
prints **TTFR** — your minutes-to-first-cited-answer (fresh-env measured: 2.9 min).
Prefer manual control? The config file it writes is plain JSON:

## 3b. Or: tell Heydey where your documents live, by hand

Your folders are **machine-local config, never code**. Create
`~/.heydey/corpus.json`:

```json
{
  "sources": [
    {"root": "~/Documents/company-docs", "glob": "**/*.md"},
    {"root": "~/Documents/decisions",    "glob": "*.md"}
  ],
  "deny_names": ["private-notes.md"]
}
```

- `sources` — `(root, glob)` pairs to ingest (markdown/text).
- `deny_names` — filenames that must never enter any corpus, on top of the
  built-in structural wall: anything under a `Personal/` directory and any
  filename containing "personal" is always excluded.
- Optional keys: `reveal_roots` (extra folders receipts may open into — source
  roots and the repo are always allowed), `blos_source` / `golden_set`
  (only for migrating an existing store; skip on a fresh install).

## 4. Ingest

```bash
api/.venv/bin/python -m heydey.ops_ingest --workspace mycompany
```

Creates the workspace on first run (one SQLite file per workspace, under
`~/.heydey/workspaces/`), slices your documents, scrubs PII, tags injection
risk, stores vectors + FTS, and extracts graph entities. Re-runs are idempotent.

## 5. Run it

```bash
# supervisor (binds 127.0.0.1 only; per-launch bearer token; port 4393)
api/.venv/bin/python api/heydey_supervisor.py &
curl -s http://127.0.0.1:4393/health

# UI
cd webapp && npm install && npm run dev   # -> http://localhost:3000
```

The UI reads the supervisor's token from `~/.heydey/runtime/supervisor.json`
server-side — the token never reaches the browser.

## 6. CLI

```bash
ln -s "$PWD/heydey" /usr/local/bin/heydey
heydey find "that pricing decision from June" --k 5
```

## 7. Optional: run at login (launchd)

The plists in `launchd/` are templates:

```bash
sed -e "s|__HEYDEY_ROOT__|$PWD|g" -e "s|__HEYDEY_HOME__|$HOME/.heydey|g" \
    launchd/ai.heydey.supervisor.plist > ~/Library/LaunchAgents/ai.heydey.supervisor.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.heydey.supervisor.plist
```

## 8. Verify the proofs (optional but encouraged)

```bash
api/.venv/bin/python -m pytest api/tests -o addopts=""   # full suite
api/.venv/bin/python -m heydey.s2_gate --workspace mycompany   # retrieval gate
```

Every build slice has a falsifiable gate runner (`python -m heydey.s2_gate`,
`s3_eval`, `s4a_gate` … `s7_gate`). Green gates are the definition of done here.

## Troubleshooting

- `No corpus sources configured` — create `~/.heydey/corpus.json` (step 3).
- `workspace does not exist` — run the ingest (step 4) or `POST /workspaces`.
- Validator refuses everything — check both Ollama models are pulled and the
  executor/validator are from different families (the Models panel enforces this
  at save; it is not user-disableable by design).
