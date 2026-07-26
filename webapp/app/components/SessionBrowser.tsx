"use client";

// F5 — Session Browser core (A11): an intent-card stream over the episodic log.
// Search by MEANING of the run ("police quote"), card click Z-drills to the
// session's receipts, delete = the right to forget (session + receipts).
// States: loading / empty-with-CTA / loaded / error-with-next-step.

import { useCallback, useEffect, useState } from "react";

type SessionCard = {
  run_id: string;
  intent: string;
  at: string;
  duration: number;
  cost: number;
  project: string;
  receipts: number;
};

type Receipt = {
  sentence_index: number;
  claim_text: string;
  doc_id: string;
  retrieval_score: number | null;
  confidence_band: string;
  validator_pass: number | null;
  model: string;
  created_at: string;
};

type Detail = SessionCard & { receipts: Receipt[] };

function basename(path: string): string {
  return String(path ?? "").split("/").filter(Boolean).pop() ?? String(path ?? "");
}

export function SessionBrowser() {
  const [query, setQuery] = useState("");
  const [cards, setCards] = useState<SessionCard[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<Detail | null>(null);
  const [confirmDelete, setConfirmDelete] = useState("");

  const load = useCallback(async (q: string) => {
    try {
      const response = await fetch(`/api/sessions?q=${encodeURIComponent(q)}`);
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setCards(body.sessions as SessionCard[]);
      setPhase("ready");
      setError("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPhase("error");
    }
  }, []);

  useEffect(() => {
    void load("");
  }, [load]);

  async function drill(runId: string) {
    const response = await fetch(`/api/sessions?run_id=${encodeURIComponent(runId)}`);
    if (response.ok) setOpen((await response.json()) as Detail);
  }

  async function forget(runId: string) {
    if (confirmDelete !== runId) {
      setConfirmDelete(runId); // first tap arms, second tap forgets
      return;
    }
    const response = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_id: runId }),
    });
    if (response.ok) {
      setConfirmDelete("");
      setOpen(null);
      void load(query);
    }
  }

  return (
    <div>
      <form
        className="summon"
        onSubmit={(event) => {
          event.preventDefault();
          void load(query);
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
          placeholder="Recall a run by intent — try “police quote”…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Recall a run by intent"
        />
      </form>

      {phase === "loading" && (
        <div className="pulse" style={{ marginTop: 24, color: "var(--text-muted)", fontSize: 13.5 }}>
          loading the run log…
        </div>
      )}

      {phase === "error" && (
        <div className="plate rise" style={{ marginTop: 20, padding: "16px 20px", borderColor: "rgba(232,161,60,.35)" }}>
          <div style={{ color: "var(--conf-warn)", fontSize: 13, fontWeight: 550 }}>Sessions unavailable</div>
          <div className="receipt" style={{ marginTop: 4 }}>{error}</div>
        </div>
      )}

      {phase === "ready" && cards.length === 0 && (
        <div className="plate rise" style={{ marginTop: 20, padding: "18px 22px" }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>No runs recorded yet</div>
          <div style={{ marginTop: 4, color: "var(--text-muted)", fontSize: 13 }}>
            Ask anything on the Ask tab — every run logs itself here with its receipts.
          </div>
        </div>
      )}

      {phase === "ready" && cards.length > 0 && (
        <div style={{ marginTop: 18 }}>
          <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginBottom: 10 }}>
            {cards.length} run(s){query ? ` matching “${query}”` : " · most recent first"}
          </div>
          {cards.map((card, index) => (
            <div
              key={card.run_id}
              className="plate reveal"
              style={{ "--i": Math.min(index, 8), padding: "13px 18px", marginBottom: 9, cursor: "pointer" } as React.CSSProperties}
              onClick={() => void drill(card.run_id)}
            >
              <div style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
                <span style={{ fontSize: 13.5, letterSpacing: "-0.013em", flex: 1, minWidth: 220 }}>
                  {card.intent || <span style={{ color: "var(--text-faint)" }}>(no intent recorded)</span>}
                </span>
                <span className="receipt">
                  {card.at?.slice(5, 16)} · {card.duration?.toFixed(1)}s · $
                  {(card.cost ?? 0).toFixed(4)} ·{" "}
                  <span style={{ color: card.receipts ? "var(--conf-validated)" : "var(--text-faint)" }}>
                    {card.receipts} receipt(s)
                  </span>
                </span>
                <button
                  type="button"
                  className="chip"
                  style={{
                    padding: "2px 10px",
                    fontSize: 11,
                    ...(confirmDelete === card.run_id
                      ? { borderColor: "rgba(232,161,60,.5)", color: "var(--conf-warn)" }
                      : {}),
                  }}
                  onClick={(event) => {
                    event.stopPropagation();
                    void forget(card.run_id);
                  }}
                >
                  {confirmDelete === card.run_id ? "tap again to forget" : "forget"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {open && (
        <div className="plate rise" style={{ marginTop: 14, padding: "18px 22px" }}>
          <div style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
            <span style={{ fontSize: 14.5, fontWeight: 600, letterSpacing: "-0.013em", flex: 1 }}>
              {open.intent}
            </span>
            <span className="receipt">{open.at} · run {open.run_id.slice(0, 12)}</span>
          </div>
          {open.receipts.length > 0 ? (
            <div style={{ marginTop: 10 }}>
              <div className="receipt" style={{ color: "var(--conf-validated)" }}>
                what this run can prove
              </div>
              {open.receipts.map((receipt, i) => (
                <div key={i} className="receipt reveal" style={{ "--i": i } as React.CSSProperties}>
                  {receipt.claim_text?.slice(0, 120)}
                  {" · "}
                  [{basename(receipt.doc_id)}
                  {receipt.retrieval_score != null ? ` · ${receipt.retrieval_score.toFixed(2)}` : ""}
                  {receipt.validator_pass != null ? (receipt.validator_pass ? " · ✓" : " · ✗") : ""}]
                </div>
              ))}
            </div>
          ) : (
            <div className="receipt" style={{ marginTop: 10, color: "var(--text-faint)" }}>
              no receipts on this run (evidence-only or empty result)
            </div>
          )}
        </div>
      )}
    </div>
  );
}
