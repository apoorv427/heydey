"use client";

// F1 — the Ask surface. Staged honestly: evidence paints instantly (retrieval,
// no LLM), then the cross-model validated synthesis lands and the seal stamps.
// Four states: empty-with-CTA / retrieving+validating (ingesting) / loaded / error.

import { useEffect, useRef, useState } from "react";

type Citation = {
  source: string;
  path: string;
  chunk: number;
  date: string;
  score: number | null;
  snippet: string;
  flagged: boolean;
};

type Evidence = {
  citations: Citation[];
  preview: string;
  latency_ms: number;
  profile: string;
};

type FullResult = {
  answer: string;
  answer_kind: string;
  badge: string;
  validator_pass: boolean | null;
  validator_status: string;
  executor_model: string;
  validator_model: string;
  citations: Citation[];
  receipts: unknown[];
  ungrounded_count: number;
  cost_usd: number;
  duration_s: number;
  retry_used: boolean;
};

const EXAMPLES = [
  "What did we decide about pricing?",
  "What changed in the last status update?",
  "What does the wrapper ban forbid?",
];

function basename(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? path;
}

function scoreClass(score: number | null): string {
  if (score == null) return "score-warn";
  if (score >= 0.7) return "score-high";
  if (score >= 0.6) return "score-mid";
  if (score >= 0.5) return "score-low";
  return "score-warn";
}

function ReceiptLine({ cite, index }: { cite: Citation; index: number }) {
  return (
    <div className="receipt reveal" style={{ "--i": index } as React.CSSProperties}>
      <span className={scoreClass(cite.score)}>
        [KB: {basename(cite.source)} · chunk {cite.chunk}
        {cite.date ? ` · ${cite.date}` : ""}
        {cite.score != null ? ` · score ${cite.score.toFixed(3)}` : ""}]
      </span>
    </div>
  );
}

export function AskSurface() {
  const [query, setQuery] = useState("");
  const [asked, setAsked] = useState("");
  const [phase, setPhase] = useState<"idle" | "retrieving" | "validating" | "done" | "error">(
    "idle",
  );
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [full, setFull] = useState<FullResult | null>(null);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typingTarget =
        document.activeElement instanceof HTMLInputElement ||
        document.activeElement instanceof HTMLTextAreaElement;
      if ((event.metaKey && event.key.toLowerCase() === "k") || (event.key === "/" && !typingTarget)) {
        event.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function run(question: string) {
    const q = question.trim();
    if (!q) return;
    setAsked(q);
    setQuery(q);
    setPhase("retrieving");
    setEvidence(null);
    setFull(null);
    setError("");
    try {
      const evidenceResponse = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, mode: "evidence" }),
      });
      const evidenceBody = await evidenceResponse.json();
      if (!evidenceResponse.ok) throw new Error(evidenceBody.detail ?? `HTTP ${evidenceResponse.status}`);
      setEvidence(evidenceBody as Evidence);
      setPhase("validating");

      const fullResponse = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, mode: "full" }),
      });
      const fullBody = await fullResponse.json();
      if (!fullResponse.ok) throw new Error(fullBody.detail ?? `HTTP ${fullResponse.status}`);
      setFull(fullBody as FullResult);
      setPhase("done");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPhase("error");
    }
  }

  const citations = full?.citations?.length ? full.citations : evidence?.citations ?? [];
  const sealTone =
    full == null
      ? "var(--conf-warn)"
      : full.validator_pass && full.answer_kind === "synthesized"
        ? "var(--conf-validated)"
        : "var(--conf-warn)";
  const sealLabel =
    full == null
      ? ""
      : full.answer_kind === "synthesized"
        ? `${full.badge}`
        : `${full.badge} · ${full.answer_kind}`;

  return (
    <div>
      {/* Summon slab — vibrancy is an accent here only (kill list: no glass fields) */}
      <form
        className="summon"
        onSubmit={(event) => {
          event.preventDefault();
          void run(query);
        }}
        style={{
          background: "rgba(13, 20, 38, 0.72)",
          backdropFilter: "blur(18px) saturate(1.4)",
          WebkitBackdropFilter: "blur(18px) saturate(1.4)",
          border: "1px solid var(--plate-border)",
          borderRadius: "var(--radius)",
          boxShadow: "0 18px 48px rgba(0, 0, 0, 0.42)",
        }}
      >
        <input
          ref={inputRef}
          className="slab-input"
          placeholder="Ask your business"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Ask your business"
        />
      </form>

      {phase === "idle" && (
        <div className="rise" style={{ marginTop: 28 }}>
          <div style={{ color: "var(--text-muted)", fontSize: 13.5, lineHeight: 1.7 }}>
            Every answer is retrieved from your corpus, then checked by a different model
            family before it may call itself validated. Receipts on everything.
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 16 }}>
            {EXAMPLES.map((example) => (
              <button key={example} className="chip" type="button" onClick={() => void run(example)}>
                {example}
              </button>
            ))}
          </div>
          <div style={{ marginTop: 18, fontSize: 11.5, color: "var(--text-faint)" }}>
            ⌘K or / to summon · answers carry [file · chunk · date · score] breadcrumbs
          </div>
        </div>
      )}

      {phase === "error" && (
        <div
          className="plate rise"
          style={{ marginTop: 24, padding: "18px 22px", borderColor: "rgba(232,161,60,.35)" }}
        >
          <div style={{ color: "var(--conf-warn)", fontSize: 13.5, fontWeight: 550 }}>
            Couldn&apos;t complete the ask
          </div>
          <div className="receipt" style={{ marginTop: 6 }}>{error}</div>
        </div>
      )}

      {(phase === "retrieving" || phase === "validating" || phase === "done") && (
        <div className="plate rise" style={{ marginTop: 24, padding: "22px 26px" }}>
          <div style={{ fontSize: 12, color: "var(--text-faint)", marginBottom: 12 }}>{asked}</div>

          {phase === "retrieving" && (
            <div className="pulse" style={{ color: "var(--text-muted)", fontSize: 13.5 }}>
              retrieving evidence…
            </div>
          )}

          {(phase === "validating" || phase === "done") && (
            <>
              <div style={{ fontSize: 15, lineHeight: 1.75, whiteSpace: "pre-wrap" }}>
                {phase === "done" && full ? full.answer : evidence?.preview}
              </div>

              {phase === "validating" && (
                <div
                  className="pulse"
                  style={{ marginTop: 14, fontSize: 12, color: "var(--text-muted)" }}
                >
                  top evidence shown verbatim · synthesizing + validating cross-model…
                </div>
              )}

              <div style={{ marginTop: 16, borderTop: "1px solid var(--plate-border)", paddingTop: 12 }}>
                {citations.slice(0, 6).map((cite, index) => (
                  <ReceiptLine key={`${cite.source}-${cite.chunk}-${index}`} cite={cite} index={index} />
                ))}
              </div>

              {phase === "done" && full && (
                <div
                  style={{
                    marginTop: 16,
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    flexWrap: "wrap",
                  }}
                >
                  <span
                    className="seal"
                    style={{
                      border: `1px solid ${sealTone}`,
                      color: sealTone,
                      borderRadius: 999,
                      padding: "4px 12px",
                      fontSize: 12,
                      fontFamily: "var(--mono)",
                    }}
                  >
                    {sealLabel}
                  </span>
                  <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>
                    evidence {evidence ? `${Math.round(evidence.latency_ms)}ms` : "—"} · full{" "}
                    {full.duration_s.toFixed(1)}s · ${full.cost_usd.toFixed(4)} ·{" "}
                    {full.retry_used ? "1 retry · " : ""}
                    {full.ungrounded_count} ungrounded
                  </span>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
