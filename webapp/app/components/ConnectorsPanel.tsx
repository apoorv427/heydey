"use client";

// F6 — Connectors (Scene A6+A7): known local MCP servers → Connect (register) →
// Sync now (pull loop, spinner, then the sync-report line), and the LIVE MAP —
// one plate per connected source showing results · flagged · last_sync · chunks.
// The flagged count is rendered in conf-warn WITH the number: the injection guard
// made visible, never hidden. After a sync, a chip links to /today (the brief now
// carries the d2c-ops section). States: loading / empty-with-CTA / loaded / error.
//
// S6c: additive props (§D) — `workspace` swaps the hardcoded body value so the
// same panel drives the /connectors page AND the /agents Connect scene against a
// non-blueleaf workspace; `servers` filters the offered known list to the
// selected playbook's connectors. Defaults ("blueleaf" / undefined) reproduce
// the pre-S6c behaviour exactly — zero visual change on /connectors.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

type LiveRow = {
  connector_id: string;
  keychain_ref: string;
  scopes: string;
  results: number;
  flagged: number;
  last_sync: string | null;
  chunks: number;
};

type SyncReport = {
  connector_id: string;
  tools_pulled: number;
  chunks: number;
  flagged: number;
  entities: number;
  synced_at: string;
};

type ConnectorsData = { connectors: LiveRow[]; known: string[] };

// Human labels for the known local servers; unknown ids fall back to the raw id.
const SERVER_LABEL: Record<string, string> = {
  "demo-shopify": "Shopify (demo store)",
  "demo-sheets": "Google Sheets (demo)",
};

const SERVER_NOTE: Record<string, string> = {
  "demo-shopify": "orders · line items · RTO — synthetic, PII-free (§14-C5)",
  "demo-sheets": "ad-spend rows · channel CAC — synthetic, PII-free (§14-C5)",
};

function shortDate(iso: string | null): string {
  if (!iso) return "never";
  return iso.length >= 16 ? iso.slice(0, 16).replace("T", " ") : iso;
}

type ConnectorsPanelProps = {
  workspace?: string;
  servers?: string[];
  // Fires after a successful sync/register so an embedding surface (e.g. Foundry)
  // can refetch downstream state (S6c: the Interview reads `scan.clean_sources`
  // from /foundry/status — its cached copy went stale after a sync completed
  // and the "sync a source first" error surfaced despite a clean sync above).
  onSyncComplete?: (connectorId: string) => void;
};

export function ConnectorsPanel({
  workspace = "blueleaf",
  servers,
  onSyncComplete,
}: ConnectorsPanelProps = {}) {
  const [data, setData] = useState<ConnectorsData | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<Record<string, "registering" | "syncing">>({});
  const [reports, setReports] = useState<Record<string, SyncReport>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const response = await fetch(
        `/api/connectors?workspace=${encodeURIComponent(workspace)}`,
      );
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setData(body as ConnectorsData);
      setPhase("ready");
      setError("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPhase("error");
    }
  }, [workspace]);

  useEffect(() => {
    void load();
  }, [load]);

  // Hooks must run unconditionally, so filter derivation lives up here — the
  // early loading/error returns below would otherwise short-circuit the hook order.
  const allKnown = data?.known ?? [];
  const known = useMemo(
    () => (servers ? allKnown.filter((id) => servers.includes(id)) : allKnown),
    [allKnown, servers],
  );

  async function post(action: "register" | "sync", connectorId: string) {
    setBusy((previous) => ({ ...previous, [connectorId]: action === "sync" ? "syncing" : "registering" }));
    setNotes((previous) => ({ ...previous, [connectorId]: "" }));
    try {
      const response = await fetch("/api/connectors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, workspace, connector_id: connectorId }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      if (action === "sync") {
        setReports((previous) => ({ ...previous, [connectorId]: body as SyncReport }));
      }
      await load(); // Live Map reflects the new register / synced counts
      onSyncComplete?.(connectorId);  // let a parent (Foundry) refetch its status
    } catch (caught) {
      setNotes((previous) => ({
        ...previous,
        [connectorId]: caught instanceof Error ? caught.message : String(caught),
      }));
    } finally {
      setBusy((previous) => {
        const next = { ...previous };
        delete next[connectorId];
        return next;
      });
    }
  }

  if (phase === "loading") {
    return (
      <div className="pulse" style={{ marginTop: 28, color: "var(--text-muted)", fontSize: 13.5 }}>
        loading connectors…
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="plate rise" style={{ marginTop: 24, padding: "18px 22px", borderColor: "rgba(232,161,60,.35)" }}>
        <div style={{ color: "var(--conf-warn)", fontSize: 13.5, fontWeight: 550 }}>Connectors unavailable</div>
        <div className="receipt" style={{ marginTop: 6 }}>{error}</div>
      </div>
    );
  }

  const live = data?.connectors ?? [];
  const connectedIds = new Set(live.map((c) => c.connector_id));
  const syncedAny = Object.keys(reports).length > 0;

  return (
    <div>
      {/* Scene A6 — known local servers → Connect → Sync now */}
      <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-0.013em", marginTop: 18, marginBottom: 10 }}>
        Sources
        <span className="receipt" style={{ marginLeft: 10, color: "var(--text-faint)" }}>
          {known.length} local server(s) · {connectedIds.size} connected
        </span>
      </div>

      {known.map((id, index) => {
        const connected = connectedIds.has(id);
        const state = busy[id];
        const report = reports[id];
        const note = notes[id];
        return (
          <div
            key={id}
            className="plate reveal"
            style={{ "--i": Math.min(index, 8), padding: "16px 20px", marginBottom: 10 } as React.CSSProperties}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
              <span style={{ fontSize: 14.5, fontWeight: 600, letterSpacing: "-0.013em" }}>
                {SERVER_LABEL[id] ?? id}
              </span>
              <span className="receipt">
                {id}
                {connected ? " · connected" : " · not connected"}
              </span>
            </div>
            <div style={{ marginTop: 4, color: "var(--text-muted)", fontSize: 12.5 }}>
              {SERVER_NOTE[id] ?? "local MCP server"}
            </div>

            <div style={{ display: "flex", gap: 10, marginTop: 14, alignItems: "center", flexWrap: "wrap" }}>
              {!connected && (
                <button
                  type="button"
                  className="chip"
                  disabled={state != null}
                  onClick={() => void post("register", id)}
                >
                  {state === "registering" ? "connecting…" : "Connect"}
                </button>
              )}
              <button
                type="button"
                className="chip"
                disabled={state != null}
                style={connected ? { borderColor: "rgba(79,216,196,.4)", color: "var(--conf-validated)" } : undefined}
                onClick={() => void post("sync", id)}
              >
                {state === "syncing" ? (
                  <span className="pulse">syncing…</span>
                ) : connected ? (
                  "Sync now"
                ) : (
                  "Connect & sync"
                )}
              </button>

              {report && (
                <span className="receipt seal">
                  pulled {report.tools_pulled} tool(s) ·{" "}
                  <span style={{ color: "var(--conf-validated)" }}>{report.chunks} chunk(s)</span> ·{" "}
                  <span style={{ color: report.flagged > 0 ? "var(--conf-warn)" : "var(--text-faint)" }}>
                    flagged {report.flagged}
                  </span>{" "}
                  · {report.entities} entities · {shortDate(report.synced_at)}
                </span>
              )}
            </div>

            {note && (
              <div className="receipt" style={{ marginTop: 8, color: "var(--conf-warn)" }}>
                {note}
              </div>
            )}
          </div>
        );
      })}

      {/* Scene A7 — the LIVE MAP */}
      <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-0.013em", marginTop: 24, marginBottom: 10 }}>
        Live Map
        <span className="receipt" style={{ marginLeft: 10, color: "var(--text-faint)" }}>
          {live.length} connected source(s)
        </span>
      </div>

      {live.length === 0 ? (
        <div className="plate rise" style={{ padding: "18px 22px" }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>Nothing connected yet</div>
          <div style={{ marginTop: 4, color: "var(--text-muted)", fontSize: 13 }}>
            Connect a source above and sync it — each pull runs through the injection guard,
            lands clean rows in the corpus, and appears here with its live counts.
          </div>
        </div>
      ) : (
        live.map((row, index) => (
          <div
            key={row.connector_id}
            className="plate reveal"
            style={{ "--i": Math.min(index, 8), padding: "16px 20px", marginBottom: 10 } as React.CSSProperties}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
              <span style={{ fontSize: 14.5, fontWeight: 600, letterSpacing: "-0.013em" }}>
                {SERVER_LABEL[row.connector_id] ?? row.connector_id}
              </span>
              <span className="receipt" style={{ color: "var(--text-faint)" }}>
                {row.keychain_ref}
              </span>
            </div>
            <div className="receipt" style={{ marginTop: 8, display: "flex", gap: 14, flexWrap: "wrap" }}>
              <span>
                results <span style={{ color: "var(--text)" }}>{row.results}</span>
              </span>
              <span style={{ color: row.flagged > 0 ? "var(--conf-warn)" : "var(--text-faint)" }}>
                flagged {row.flagged}
              </span>
              <span>
                corpus{" "}
                <span style={{ color: row.chunks > 0 ? "var(--conf-validated)" : "var(--text-faint)" }}>
                  {row.chunks} chunk(s)
                </span>
              </span>
              <span style={{ color: "var(--text-faint)" }}>last sync {shortDate(row.last_sync)}</span>
            </div>
          </div>
        ))
      )}

      {/* After a sync — send the fresh d2c-ops data through the overnight pass */}
      {syncedAny && (
        <div style={{ display: "flex", gap: 10, marginTop: 16, alignItems: "center", flexWrap: "wrap" }}>
          <Link
            href="/today"
            className="chip"
            style={{ textDecoration: "none", borderColor: "rgba(79,216,196,.4)", color: "var(--conf-validated)" }}
          >
            run overnight pass →
          </Link>
          <span className="receipt" style={{ color: "var(--text-faint)" }}>
            the Morning Brief now carries a d2c-ops section from these rows
          </span>
        </div>
      )}
    </div>
  );
}
