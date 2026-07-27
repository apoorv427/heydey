"use client";

// Builder B — Files: "what did my AI just make, and where did it come from?"
// Surfaces heydey/artifacts.py's two families of file, never conflated:
//   - Heydey-produced artifacts, each carrying real provenance (run, approval,
//     risk tier, receipt claim) or an honest "no provenance recorded".
//   - Recent files found by a query-time OS scan — explicitly NOT made by
//     Heydey, provenance always null.
// States: loading / empty-with-CTA / error-with-next-step / loaded.

import { useCallback, useEffect, useState } from "react";

type Receipt = { claim_text: string; created_at: string } | null;

type HeydeyArtifact = {
  path: string;
  name: string;
  kind: string;
  bytes: number;
  created_at: string;
  source: "heydey";
  run_id: string | null;
  approval_id: number | null;
  title: string | null;
  risk_tier: string | null;
  receipt: Receipt;
  sources: unknown[];
};

type OsFile = {
  path: string;
  name: string;
  kind: string;
  bytes: number;
  created_at: string;
  source: "os";
  run_id: null;
  approval_id: null;
  title: null;
  risk_tier: null;
  receipt: null;
  sources: never[];
};

type Summary = {
  state: "loaded" | "empty" | "error";
  heydey_count: number;
  os_count: number;
  roots_configured: string[];
  next_step: string;
};

type ArtifactsPayload = {
  summary: Summary;
  artifacts: HeydeyArtifact[];
  os_files?: OsFile[];
};

function basename(path: string): string {
  return String(path ?? "").split("/").filter(Boolean).pop() ?? String(path ?? "");
}

function humanBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let value = n / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`;
}

function whenLabel(iso: string): string {
  if (!iso) return "unknown time";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffMs = Date.now() - d.getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 14) return `${days}d ago`;
  return d.toISOString().slice(0, 10);
}

const RISK_COLOR: Record<string, string> = {
  read: "var(--conf-validated)",
  write_local: "var(--conf-high)",
  exec: "var(--conf-low)",
  external: "var(--conf-warn)",
};

function RiskChip({ tier }: { tier: string }) {
  return (
    <span
      className="receipt"
      style={{
        color: RISK_COLOR[tier] ?? "var(--text-muted)",
        border: `1px solid ${RISK_COLOR[tier] ?? "var(--plate-border)"}`,
        borderRadius: 999,
        padding: "1px 8px",
        fontSize: 10.5,
      }}
    >
      {tier}
    </span>
  );
}

function RevealButton({ path }: { path: string }) {
  const [note, setNote] = useState("");

  async function reveal() {
    setNote("revealing…");
    try {
      const response = await fetch("/api/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const body = await response.json();
      setNote(response.ok ? "revealed in Finder" : (body.detail ?? `HTTP ${response.status}`));
    } catch (caught) {
      setNote(caught instanceof Error ? caught.message : String(caught));
    }
  }

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <button
        type="button"
        className="chip"
        style={{ padding: "2px 10px", fontSize: 11 }}
        onClick={(event) => {
          event.stopPropagation();
          void reveal();
        }}
      >
        reveal in Finder
      </button>
      {note && <span className="receipt">{note}</span>}
    </div>
  );
}

function HeydeyRow({ item, index }: { item: HeydeyArtifact; index: number }) {
  const hasProvenance = Boolean(item.receipt || item.run_id || item.approval_id);
  return (
    <div
      className="plate reveal"
      style={{ "--i": Math.min(index, 8), padding: "13px 18px", marginBottom: 9 } as React.CSSProperties}
    >
      <div style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
        <span style={{ fontSize: 13.5, letterSpacing: "-0.013em", flex: 1, minWidth: 220 }}>
          {item.title || item.name}
        </span>
        {item.risk_tier && <RiskChip tier={item.risk_tier} />}
        <span className="receipt">
          {item.kind} · {humanBytes(item.bytes)} · {whenLabel(item.created_at)}
        </span>
      </div>
      <div className="receipt" style={{ marginTop: 4 }}>{item.path}</div>

      {hasProvenance ? (
        <div style={{ marginTop: 8, paddingTop: 8, borderTop: "1px solid var(--plate-border)" }}>
          <div className="receipt" style={{ color: "var(--conf-validated)" }}>
            {item.run_id ? `run ${item.run_id.slice(0, 16)}` : "run unknown"}
            {item.approval_id != null ? ` · approval #${item.approval_id}` : ""}
          </div>
          {item.receipt && (
            <div className="receipt" style={{ marginTop: 2 }}>
              &ldquo;{item.receipt.claim_text?.slice(0, 140)}&rdquo;
            </div>
          )}
        </div>
      ) : (
        <div
          className="receipt"
          style={{ marginTop: 8, paddingTop: 8, borderTop: "1px solid var(--plate-border)", color: "var(--text-faint)" }}
        >
          no provenance recorded
        </div>
      )}

      <div style={{ marginTop: 8 }}>
        <RevealButton path={item.path} />
      </div>
    </div>
  );
}

function OsRow({ item, index }: { item: OsFile; index: number }) {
  return (
    <div
      className="plate reveal"
      style={{ "--i": Math.min(index, 8), padding: "13px 18px", marginBottom: 9 } as React.CSSProperties}
    >
      <div style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
        <span style={{ fontSize: 13.5, letterSpacing: "-0.013em", flex: 1, minWidth: 220 }}>
          {item.name}
        </span>
        <span className="receipt">
          {item.kind} · {humanBytes(item.bytes)} · {whenLabel(item.created_at)}
        </span>
      </div>
      <div className="receipt" style={{ marginTop: 4 }}>{item.path}</div>
      <div className="receipt" style={{ marginTop: 8, color: "var(--text-faint)" }}>
        found in your folders — not produced by Heydey, provenance null
      </div>
      <div style={{ marginTop: 8 }}>
        <RevealButton path={item.path} />
      </div>
    </div>
  );
}

export function FilesView() {
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [data, setData] = useState<ArtifactsPayload | null>(null);

  const load = useCallback(async () => {
    setPhase("loading");
    try {
      const response = await fetch("/api/artifacts?limit=50&include_os=true");
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setData(body as ArtifactsPayload);
      setPhase("ready");
      setError("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPhase("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (phase === "loading") {
    return (
      <div className="pulse" style={{ marginTop: 24, color: "var(--text-muted)", fontSize: 13.5 }}>
        scanning artifacts…
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="plate rise" style={{ marginTop: 20, padding: "16px 20px", borderColor: "rgba(232,161,60,.35)" }}>
        <div style={{ color: "var(--conf-warn)", fontSize: 13, fontWeight: 550 }}>Files unavailable</div>
        <div className="receipt" style={{ marginTop: 4 }}>{error}</div>
        <button
          type="button"
          className="chip"
          style={{ marginTop: 10 }}
          onClick={() => void load()}
        >
          retry
        </button>
      </div>
    );
  }

  const summary = data!.summary;

  if (summary.state === "error") {
    return (
      <div className="plate rise" style={{ marginTop: 20, padding: "16px 20px", borderColor: "rgba(232,161,60,.35)" }}>
        <div style={{ color: "var(--conf-warn)", fontSize: 13, fontWeight: 550 }}>Could not read artifacts</div>
        <div className="receipt" style={{ marginTop: 4 }}>{summary.next_step}</div>
        <button
          type="button"
          className="chip"
          style={{ marginTop: 10 }}
          onClick={() => void load()}
        >
          retry
        </button>
      </div>
    );
  }

  if (summary.state === "empty") {
    return (
      <div className="plate rise" style={{ marginTop: 20, padding: "18px 22px" }}>
        <div style={{ fontSize: 14, fontWeight: 600 }}>Nothing here yet</div>
        <div style={{ marginTop: 4, color: "var(--text-muted)", fontSize: 13 }}>{summary.next_step}</div>
        <div className="receipt" style={{ marginTop: 10 }}>
          scanned roots: {summary.roots_configured.join(", ") || "(none configured)"}
        </div>
      </div>
    );
  }

  const heydeyItems = data!.artifacts ?? [];
  const osItems = data!.os_files ?? [];

  return (
    <div style={{ marginTop: 18 }}>
      <section>
        <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginBottom: 10 }}>
          Made by Heydey · {heydeyItems.length} file(s)
        </div>
        {heydeyItems.length === 0 ? (
          <div className="plate" style={{ padding: "14px 18px", marginBottom: 20 }}>
            <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
              No Heydey-produced artifacts yet. Approve a prepared action to create the first one.
            </div>
          </div>
        ) : (
          <div style={{ marginBottom: 20 }}>
            {heydeyItems.map((item, i) => (
              <HeydeyRow key={item.path} item={item} index={i} />
            ))}
          </div>
        )}
      </section>

      <section>
        <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginBottom: 10 }}>
          Recent in your folders (not produced by Heydey) · {osItems.length} file(s)
        </div>
        {osItems.length === 0 ? (
          <div className="plate" style={{ padding: "14px 18px" }}>
            <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
              No recent files found under {summary.roots_configured.join(", ") || "the configured roots"}.
            </div>
          </div>
        ) : (
          <div>
            {osItems.map((item, i) => (
              <OsRow key={item.path} item={item} index={i} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
