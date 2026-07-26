"use client";

// F3 — Today: the Morning Brief plate (A8) + the Approvals tray with the
// prepared-action card (A9). Approve emits artifact + admin deep-link + receipt;
// the footnote "no write was fired" is the L34 claim rendered as UI.
// States: no-brief-yet (CTA) / running / loaded / error-with-next-step.

import { useCallback, useEffect, useState } from "react";

type Breadcrumb = { source: string; chunk: number | null; date: string };
type BriefItem = { section: string; line: string; breadcrumb: Breadcrumb };
type Brief = {
  id: number;
  kind: string;
  items: BriefItem[];
  created_at: string;
  notified: boolean;
};

type Sku = {
  sku: string;
  product_id: number;
  title: string;
  return_rate: number;
  units_30d: number;
};

type Approval = {
  id: number;
  action_class: string;
  status: string;
  requested_at: string;
  payload: {
    kind: string;
    title: string;
    demo?: boolean;
    store_handle: string;
    base_return_rate: number;
    skus: Sku[];
  };
};

type Decision = {
  id: number;
  status: string;
  artifact?: string;
  deep_link?: string;
  note?: string;
};

type SentinelReport = { summary: string; flags: string[] };

type TodayData = {
  brief: Brief | null;
  approvals: Approval[];
  sentinel: SentinelReport;
};

function basename(path: string): string {
  return String(path).split("/").filter(Boolean).pop() ?? String(path);
}

function Crumb({ crumb }: { crumb: Breadcrumb }) {
  const chunk = crumb.chunk != null ? ` · chunk ${crumb.chunk}` : "";
  const date = crumb.date ? ` · ${crumb.date}` : "";
  return (
    <span className="receipt" style={{ color: "var(--text-faint)" }}>
      [{basename(crumb.source)}
      {chunk}
      {date}]
    </span>
  );
}

const SECTION_LABEL: Record<string, string> = {
  changed: "changed overnight",
  gates: "open gates",
  locks: "locks touched",
  sentinel: "system health",
  costs: "spend",
  sessions: "yesterday",
  approvals: "tray",
};

export function TodayView() {
  const [data, setData] = useState<TodayData | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "running" | "error">("loading");
  const [error, setError] = useState("");
  const [decisions, setDecisions] = useState<Record<number, Decision>>({});

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/today");
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setData(body as TodayData);
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

  async function simulateOvernight() {
    setPhase("running");
    try {
      const response = await fetch("/api/brief", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace: "blueleaf", notify: true }),
      });
      if (!response.ok) throw new Error((await response.json()).detail ?? `HTTP ${response.status}`);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPhase("error");
    }
  }

  async function decideApproval(id: number, decision: "approved" | "denied") {
    const response = await fetch("/api/approvals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, decision, workspace: "blueleaf" }),
    });
    if (response.ok) {
      const body = (await response.json()) as Decision;
      setDecisions((previous) => ({ ...previous, [id]: body }));
      setData((previous) =>
        previous
          ? { ...previous, approvals: previous.approvals.filter((a) => a.id !== id) }
          : previous,
      );
    }
  }

  if (phase === "loading") {
    return <div className="pulse" style={{ marginTop: 28, color: "var(--text-muted)", fontSize: 13.5 }}>loading Today…</div>;
  }

  if (phase === "error") {
    return (
      <div className="plate rise" style={{ marginTop: 24, padding: "18px 22px", borderColor: "rgba(232,161,60,.35)" }}>
        <div style={{ color: "var(--conf-warn)", fontSize: 13.5, fontWeight: 550 }}>Today unavailable</div>
        <div className="receipt" style={{ marginTop: 6 }}>{error}</div>
      </div>
    );
  }

  const brief = data?.brief ?? null;
  const tray = data?.approvals ?? [];
  const decided = Object.values(decisions);

  return (
    <div>
      {/* header row */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 4, flexWrap: "wrap" }}>
        <button
          type="button"
          className="chip"
          disabled={phase === "running"}
          onClick={() => void simulateOvernight()}
        >
          {phase === "running" ? "running overnight pass…" : "simulate overnight run"}
        </button>
        {data?.sentinel && (
          <span className="receipt">
            sentinel {data.sentinel.summary}
            {data.sentinel.flags.length > 0 && (
              <span style={{ color: "var(--conf-warn)" }}> · {data.sentinel.flags.length} flag(s)</span>
            )}
          </span>
        )}
      </div>

      {/* Morning Brief plate (A8) */}
      {brief == null ? (
        <div className="plate rise" style={{ marginTop: 20, padding: "22px 26px" }}>
          <div style={{ fontSize: 15, fontWeight: 600, letterSpacing: "-0.013em" }}>
            No brief yet
          </div>
          <div style={{ marginTop: 6, color: "var(--text-muted)", fontSize: 13 }}>
            Run the overnight pass once — it assembles a cited brief from LOCKS, STATUS and
            memory, with a breadcrumb on every line. Install the 07:00 schedule with{" "}
            <span className="receipt">python -m heydey.morning_brief --install-launchd</span>
          </div>
        </div>
      ) : (
        <div className="plate rise" style={{ marginTop: 20, padding: "22px 26px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
            <div style={{ fontSize: 16, fontWeight: 650, letterSpacing: "-0.022em" }}>
              While you slept — Ops Brief
            </div>
            <span className="receipt">
              {brief.created_at} · {brief.items.length} items · notified {brief.notified ? "✓" : "—"}
            </span>
          </div>
          <div style={{ marginTop: 14 }}>
            {brief.items.map((item, index) => (
              <div
                key={`${item.section}-${index}`}
                className="reveal"
                style={{ "--i": index, padding: "7px 0", borderTop: index === 0 ? "none" : "1px solid var(--plate-border)" } as React.CSSProperties}
              >
                <div style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
                  <span
                    className="receipt"
                    style={{ color: "var(--conf-validated)", minWidth: 118, textTransform: "lowercase" }}
                  >
                    {SECTION_LABEL[item.section] ?? item.section}
                  </span>
                  <span style={{ fontSize: 13.5, lineHeight: 1.6, flex: 1, minWidth: 260 }}>
                    {item.line}
                  </span>
                </div>
                <div style={{ marginLeft: 128 }}>
                  <Crumb crumb={item.breadcrumb} />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Approvals tray + prepared-action cards (A9) */}
      <div style={{ marginTop: 22 }}>
        <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-0.013em", marginBottom: 10 }}>
          Approvals tray
          <span className="receipt" style={{ marginLeft: 10, color: "var(--text-faint)" }}>
            {tray.length} pending
          </span>
        </div>

        {tray.length === 0 && decided.length === 0 && (
          <div className="plate" style={{ padding: "14px 20px", fontSize: 13, color: "var(--text-muted)" }}>
            Tray clear — outbound and spend actions will wait here for one tap.
          </div>
        )}

        {tray.map((approval) => (
          <div key={approval.id} className="plate rise" style={{ padding: "18px 22px", marginBottom: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
              <span style={{ fontSize: 14.5, fontWeight: 600, letterSpacing: "-0.013em" }}>
                {approval.payload.title}
              </span>
              <span className="receipt">
                {approval.action_class}
                {approval.payload.demo ? " · demo data" : ""}
              </span>
            </div>
            <div style={{ marginTop: 10 }}>
              {approval.payload.skus?.map((sku) => (
                <div key={sku.sku} className="receipt" style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                  <span style={{ minWidth: 130 }}>{sku.sku}</span>
                  <span style={{ color: "var(--text)" }}>{sku.title}</span>
                  <span className="score-warn">
                    {sku.return_rate}% returns vs {approval.payload.base_return_rate}% base
                  </span>
                  <span style={{ color: "var(--text-faint)" }}>{sku.units_30d} units/30d</span>
                </div>
              ))}
            </div>
            <div style={{ display: "flex", gap: 10, marginTop: 14, alignItems: "center" }}>
              <button
                type="button"
                className="chip"
                style={{ borderColor: "rgba(79,216,196,.4)", color: "var(--conf-validated)" }}
                onClick={() => void decideApproval(approval.id, "approved")}
              >
                Approve
              </button>
              <button type="button" className="chip" onClick={() => void decideApproval(approval.id, "denied")}>
                Dismiss
              </button>
            </div>
          </div>
        ))}

        {decided.map((decision) => (
          <div
            key={`decided-${decision.id}`}
            className="plate seal"
            style={{ padding: "16px 20px", marginBottom: 12, borderColor: decision.status === "approved" ? "rgba(79,216,196,.35)" : undefined }}
          >
            <div className="receipt" style={{ color: decision.status === "approved" ? "var(--conf-validated)" : "var(--text-muted)" }}>
              approval #{decision.id} · {decision.status}
              {decision.artifact ? ` · receipt logged · artifact ${basename(decision.artifact)}` : ""}
            </div>
            {decision.status === "approved" && decision.deep_link && (
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 8, flexWrap: "wrap" }}>
                <a
                  className="chip"
                  href={decision.deep_link}
                  target="_blank"
                  rel="noreferrer"
                  style={{ textDecoration: "none" }}
                >
                  Open in Shopify admin ↗
                </a>
                <span className="receipt" style={{ color: "var(--text-faint)" }}>
                  {decision.note}
                </span>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
