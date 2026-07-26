"use client";

// The Library bar — doc-level semantic search (shares ask.find_docs with `blos find`).
// States: empty-with-CTA / searching / loaded / error. Reveal-in-Finder resolves
// the breadcrumb through the supervisor (ops roots only, Personal-walled).

import { useState } from "react";

type Chunk = { chunk: number; score: number | null; snippet: string };
type DocResult = {
  source: string;
  path: string;
  date: string;
  best_score: number | null;
  chunks: Chunk[];
};

function basename(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? path;
}

export function LibraryBar() {
  const [query, setQuery] = useState("");
  const [phase, setPhase] = useState<"idle" | "searching" | "done" | "error">("idle");
  const [docs, setDocs] = useState<DocResult[]>([]);
  const [latency, setLatency] = useState(0);
  const [error, setError] = useState("");
  const [revealed, setRevealed] = useState("");

  async function search(raw: string) {
    const q = raw.trim();
    if (!q) return;
    setPhase("searching");
    setError("");
    setRevealed("");
    try {
      const response = await fetch("/api/find", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, k: 6 }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setDocs(body.documents as DocResult[]);
      setLatency(body.latency_ms as number);
      setPhase("done");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPhase("error");
    }
  }

  async function reveal(path: string) {
    try {
      const response = await fetch("/api/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      if (response.ok) setRevealed(path);
    } catch {
      /* non-fatal — the breadcrumb text itself is still the proof */
    }
  }

  return (
    <div>
      <form
        className="summon"
        onSubmit={(event) => {
          event.preventDefault();
          void search(query);
        }}
        style={{
          marginTop: 20,
          background: "rgba(13, 20, 38, 0.72)",
          backdropFilter: "blur(18px) saturate(1.4)",
          WebkitBackdropFilter: "blur(18px) saturate(1.4)",
          border: "1px solid var(--plate-border)",
          borderRadius: "var(--radius)",
        }}
      >
        <input
          className="slab-input"
          style={{ fontSize: 15, padding: "14px 18px" }}
          placeholder="Search the corpus by meaning…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Search the corpus"
        />
      </form>

      {phase === "idle" && (
        <div className="rise" style={{ marginTop: 20, color: "var(--text-faint)", fontSize: 12.5 }}>
          Try “pricing decision” or “validator rule” — the terminal twin is{" "}
          <span className="receipt">heydey find &quot;query&quot;</span>
        </div>
      )}

      {phase === "searching" && (
        <div className="pulse" style={{ marginTop: 24, color: "var(--text-muted)", fontSize: 13.5 }}>
          searching…
        </div>
      )}

      {phase === "error" && (
        <div
          className="plate rise"
          style={{ marginTop: 20, padding: "16px 20px", borderColor: "rgba(232,161,60,.35)" }}
        >
          <div style={{ color: "var(--conf-warn)", fontSize: 13, fontWeight: 550 }}>
            Search failed
          </div>
          <div className="receipt" style={{ marginTop: 4 }}>{error}</div>
        </div>
      )}

      {phase === "done" && (
        <div style={{ marginTop: 20 }}>
          <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginBottom: 10 }}>
            {docs.length} documents · {Math.round(latency)}ms
          </div>
          {docs.length === 0 && (
            <div className="plate rise" style={{ padding: "16px 20px", fontSize: 13, color: "var(--text-muted)" }}>
              Nothing matched by meaning. Broaden the phrase, or check Status → corpus counts.
            </div>
          )}
          {docs.map((doc, index) => (
            <div
              key={doc.path || doc.source}
              className="plate reveal"
              style={{ "--i": index, padding: "14px 20px", marginBottom: 10 } as React.CSSProperties}
            >
              <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
                <span style={{ fontSize: 14, fontWeight: 550, letterSpacing: "-0.013em" }}>
                  {basename(doc.path || doc.source)}
                </span>
                <span className="receipt">
                  [{doc.date || "—"}
                  {doc.best_score != null ? ` · score ${doc.best_score.toFixed(3)}` : ""}]
                </span>
                {doc.path && (
                  <button
                    type="button"
                    className="chip"
                    style={{ padding: "2px 10px", fontSize: 11 }}
                    onClick={() => void reveal(doc.path)}
                  >
                    {revealed === doc.path ? "revealed ✓" : "Reveal in Finder"}
                  </button>
                )}
              </div>
              {doc.chunks.map((chunk) => (
                <div key={chunk.chunk} className="receipt" style={{ marginTop: 6 }}>
                  [chunk {chunk.chunk}] {chunk.snippet.replace(/\s+/g, " ").slice(0, 140)}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
