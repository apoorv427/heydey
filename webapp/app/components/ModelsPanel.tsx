"use client";

// F7 — the Models panel. Profiles (executor → validator, family-tagged), BYO key
// (presence only, never echoed), the live cost ledger, and the misconfigure demo:
// saving a same-family pair is blocked AT SAVE with the reason spelled out (amber,
// never red/green — confidence is light temperature here).

import { useCallback, useEffect, useState } from "react";

type PairView = {
  executor: string;
  validator: string;
  executor_family: string;
  validator_family: string;
};

type ProfileView = {
  name: string;
  default: PairView;
  tasks: Record<string, PairView>;
  budget_usd: number;
};

type ModelsState = {
  active: string;
  profiles: Record<string, ProfileView>;
  keys: Record<string, boolean>;
};

type Costs = {
  today_usd: number;
  today_calls: number;
  week_usd: number;
  week_calls: number;
  recent: {
    run_id: string;
    model: string;
    tokens_in: number;
    tokens_out: number;
    cost_usd: number;
    latency_ms: number;
    created_at: string;
  }[];
};

export function ModelsPanel() {
  const [state, setState] = useState<ModelsState | null>(null);
  const [costs, setCosts] = useState<Costs | null>(null);
  const [error, setError] = useState("");
  const [blocked, setBlocked] = useState("");
  const [saving, setSaving] = useState(false);
  const [keyDraft, setKeyDraft] = useState("");
  const [keyStored, setKeyStored] = useState(false);

  const load = useCallback(async () => {
    try {
      const [modelsResponse, costsResponse] = await Promise.all([
        fetch("/api/models"),
        fetch("/api/costs"),
      ]);
      const modelsBody = await modelsResponse.json();
      if (!modelsResponse.ok) throw new Error(modelsBody.detail ?? `HTTP ${modelsResponse.status}`);
      setState(modelsBody as ModelsState);
      if (costsResponse.ok) setCosts((await costsResponse.json()) as Costs);
      setError("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function activate(name: string) {
    const response = await fetch("/api/models", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "activate", profile: name }),
    });
    if (response.ok) void load();
  }

  async function demonstrateBlockedSave() {
    // The 30-second trust demo: try to save a rubber-stamp pair, watch it refused.
    setSaving(true);
    setBlocked("");
    try {
      const response = await fetch("/api/models", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "save",
          profile_data: {
            name: "rubber-stamp",
            default: { executor: "llama3.1:8b", validator: "llama3.2:3b" },
            budget_usd: 0,
          },
        }),
      });
      const body = await response.json();
      setBlocked(
        response.status === 422
          ? `BLOCKED AT SAVE (HTTP 422): ${body.detail}`
          : `unexpected: HTTP ${response.status}`,
      );
    } finally {
      setSaving(false);
    }
  }

  async function storeKey() {
    const response = await fetch("/api/models", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "set_key", provider: "openrouter", key: keyDraft }),
    });
    if (response.ok) {
      setKeyDraft("");
      setKeyStored(true);
      void load();
    }
  }

  if (error) {
    return (
      <div
        className="plate rise"
        style={{ marginTop: 24, padding: "18px 22px", borderColor: "rgba(232,161,60,.35)" }}
      >
        <div style={{ color: "var(--conf-warn)", fontSize: 13.5, fontWeight: 550 }}>
          Models panel unavailable
        </div>
        <div className="receipt" style={{ marginTop: 6 }}>{error}</div>
      </div>
    );
  }

  if (!state) {
    return (
      <div className="pulse" style={{ marginTop: 28, color: "var(--text-muted)", fontSize: 13.5 }}>
        loading profiles…
      </div>
    );
  }

  return (
    <div style={{ marginTop: 24 }}>
      {/* profiles */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
        {Object.values(state.profiles).map((profile, index) => {
          const active = profile.name === state.active;
          return (
            <div
              key={profile.name}
              className="plate reveal"
              style={{
                "--i": index,
                padding: "16px 18px",
                borderColor: active ? "rgba(79,216,196,.45)" : undefined,
              } as React.CSSProperties}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ fontSize: 13.5, fontWeight: 600, letterSpacing: "-0.013em" }}>
                  {profile.name}
                </span>
                {active ? (
                  <span className="seal" style={{ fontSize: 10.5, color: "var(--conf-validated)", fontFamily: "var(--mono)" }}>
                    ● active
                  </span>
                ) : (
                  <button
                    type="button"
                    className="chip"
                    style={{ padding: "2px 10px", fontSize: 11 }}
                    onClick={() => void activate(profile.name)}
                  >
                    activate
                  </button>
                )}
              </div>
              <div className="receipt" style={{ marginTop: 10 }}>
                writes&nbsp;&nbsp;{profile.default.executor}
                <span style={{ color: "var(--text-faint)" }}> · {profile.default.executor_family}</span>
              </div>
              <div className="receipt">
                checks&nbsp;&nbsp;{profile.default.validator}
                <span style={{ color: "var(--text-faint)" }}> · {profile.default.validator_family}</span>
              </div>
              <div className="receipt" style={{ marginTop: 6, color: "var(--text-faint)" }}>
                budget ${profile.budget_usd.toFixed(2)}/day
                {profile.budget_usd === 0 ? " · fully local · $0" : ""}
              </div>
            </div>
          );
        })}
      </div>

      {/* the misconfigure demo — the family rule is a save-time wall, show it */}
      <div className="plate" style={{ marginTop: 18, padding: "16px 20px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
            Try to save a same-family pair (llama → llama):
          </span>
          <button type="button" className="chip" disabled={saving} onClick={() => void demonstrateBlockedSave()}>
            {saving ? "saving…" : "attempt rubber-stamp save"}
          </button>
        </div>
        {blocked && (
          <div
            className="receipt rise"
            style={{ marginTop: 10, color: "var(--conf-warn)", whiteSpace: "pre-wrap" }}
          >
            {blocked}
          </div>
        )}
      </div>

      {/* BYO key — presence only */}
      <div className="plate" style={{ marginTop: 18, padding: "16px 20px" }}>
        <div style={{ fontSize: 13.5, fontWeight: 600, letterSpacing: "-0.013em" }}>
          Cloud lanes (BYO keys)
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 10, flexWrap: "wrap" }}>
          <span className="receipt">
            openrouter ·{" "}
            {state.keys.openrouter ? (
              <span style={{ color: "var(--conf-validated)" }}>key present</span>
            ) : (
              <span style={{ color: "var(--conf-warn)" }}>no key — local-only lanes run regardless</span>
            )}
          </span>
          <input
            type="password"
            value={keyDraft}
            onChange={(event) => setKeyDraft(event.target.value)}
            placeholder="paste OpenRouter key"
            aria-label="OpenRouter API key"
            style={{
              background: "transparent",
              border: "1px solid var(--plate-border)",
              borderRadius: 8,
              padding: "6px 10px",
              fontSize: 12,
              fontFamily: "var(--mono)",
              minWidth: 220,
            }}
          />
          <button
            type="button"
            className="chip"
            style={{ padding: "4px 12px" }}
            disabled={keyDraft.trim().length < 8}
            onClick={() => void storeKey()}
          >
            store in Keychain
          </button>
          {keyStored && (
            <span className="receipt seal" style={{ color: "var(--conf-validated)" }}>
              stored · never displayed again
            </span>
          )}
        </div>
      </div>

      {/* cost ledger */}
      <div className="plate" style={{ marginTop: 18, padding: "16px 20px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
          <span style={{ fontSize: 13.5, fontWeight: 600, letterSpacing: "-0.013em" }}>Cost ledger</span>
          {costs && (
            <span className="receipt">
              today ${costs.today_usd.toFixed(4)} · {costs.today_calls} calls · 7d $
              {costs.week_usd.toFixed(4)} · {costs.week_calls} calls
            </span>
          )}
        </div>
        {costs && costs.recent.length > 0 ? (
          <div style={{ marginTop: 10, overflowX: "auto" }}>
            {costs.recent.slice(0, 8).map((row) => (
              <div key={`${row.run_id}-${row.created_at}-${row.model}`} className="receipt">
                {row.created_at?.slice(5, 16)} · {row.model} · {row.tokens_in}→{row.tokens_out} tok
                · ${row.cost_usd?.toFixed(4) ?? "0.0000"} · {Math.round(row.latency_ms ?? 0)}ms
              </div>
            ))}
          </div>
        ) : (
          <div className="receipt" style={{ marginTop: 10, color: "var(--text-faint)" }}>
            no calls yet today — run an Ask and the ledger fills itself
          </div>
        )}
      </div>
    </div>
  );
}
