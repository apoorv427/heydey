"use client";

// F4 — the read-only graph panel (Contract B render spec). Top-50 live entities
// by activity score; edges = co-retrieval weight. The layout is computed ONCE
// (static force ticks) — nothing loops except Breath (NOCTURNE). No drag
// handles. Node click = Z-drill: the entity's receipts + sessions rise below.

import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY } from "d3-force";
import { useCallback, useEffect, useState } from "react";

type Node = { id: number; label: string; type: string | null; score: number };
type Edge = { source: number; target: number; weight: number; last_seen: string };
type Health = { entities: number; edges: number; last_grown: string | null };
type Panel = { nodes: Node[]; edges: Edge[]; health: Health; latency_ms: number };

type Positioned = Node & { x: number; y: number; r: number };
type PlacedEdge = { x1: number; y1: number; x2: number; y2: number; weight: number };

type Detail = {
  id: number;
  label: string;
  type: string | null;
  source_doc_id: string;
  confidence: number;
  runs: string[];
  sessions: { id: string; intent: string; started_at: string; duration: number; cost: number }[];
  receipts: { run_id: string; claim_text: string; retrieval_score: number | null; created_at: string }[];
};

const W = 720;
const H = 440;

const TYPE_COLOR: Record<string, string> = {
  project: "var(--conf-validated)",
  slice: "var(--conf-high)",
  money: "var(--conf-mid)",
  lock: "var(--conf-low)",
  marker: "var(--conf-warn)",
  proper_noun: "var(--text-muted)",
};

function layout(nodes: Node[], edges: Edge[]): { placed: Positioned[]; lines: PlacedEdge[] } {
  const maxScore = Math.max(1, ...nodes.map((n) => n.score));
  const sims = nodes.map((n) => ({ ...n, r: 5 + 14 * Math.sqrt(n.score / maxScore) }));
  const links = edges.map((e) => ({ ...e }));
  const simulation = forceSimulation(sims as never[])
    .force("link", forceLink(links as never[])
      .id((d) => (d as unknown as Node).id)
      .distance((l) => 42 + 70 / Math.sqrt((l as unknown as Edge).weight))
      .strength(0.5))
    .force("charge", forceManyBody().strength(-110))
    .force("center", forceCenter(W / 2, H / 2))
    // gentle gravity — disconnected components must compose, not flee to corners
    .force("gx", forceX(W / 2).strength(0.07))
    .force("gy", forceY(H / 2).strength(0.09))
    .force("collide", forceCollide((d) => (d as unknown as Positioned).r + 7))
    .stop();
  for (let i = 0; i < 300; i += 1) simulation.tick();
  const placed = (sims as unknown as Positioned[]).map((n) => ({
    ...n,
    x: Math.max(n.r + 2, Math.min(W - n.r - 2, n.x)),
    y: Math.max(n.r + 2, Math.min(H - n.r - 2, n.y)),
  }));
  const byId = new Map(placed.map((n) => [n.id, n]));
  const lines: PlacedEdge[] = (links as unknown as { source: Positioned; target: Positioned; weight: number }[])
    .map((l) => ({
      x1: byId.get(l.source.id)?.x ?? 0,
      y1: byId.get(l.source.id)?.y ?? 0,
      x2: byId.get(l.target.id)?.x ?? 0,
      y2: byId.get(l.target.id)?.y ?? 0,
      weight: l.weight,
    }));
  return { placed, lines };
}

export function GraphPanel() {
  const [phase, setPhase] = useState<"loading" | "ready" | "empty" | "error">("loading");
  const [error, setError] = useState("");
  const [placed, setPlaced] = useState<Positioned[]>([]);
  const [lines, setLines] = useState<PlacedEdge[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [latency, setLatency] = useState(0);
  const [selected, setSelected] = useState<Detail | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/graph");
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      const panel = body as Panel;
      setHealth(panel.health);
      setLatency(panel.latency_ms);
      if (!panel.nodes.length) {
        setPhase("empty");
        return;
      }
      const result = layout(panel.nodes, panel.edges);
      setPlaced(result.placed);
      setLines(result.lines);
      setPhase("ready");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPhase("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function select(id: number) {
    setSelectedId(id);
    const response = await fetch(`/api/graph?entity=${id}`);
    if (response.ok) setSelected((await response.json()) as Detail);
  }

  if (phase === "loading") {
    return <div className="pulse" style={{ marginTop: 28, color: "var(--text-muted)", fontSize: 13.5 }}>growing the map…</div>;
  }
  if (phase === "error") {
    return (
      <div className="plate rise" style={{ marginTop: 24, padding: "18px 22px", borderColor: "rgba(232,161,60,.35)" }}>
        <div style={{ color: "var(--conf-warn)", fontSize: 13.5, fontWeight: 550 }}>Graph unavailable</div>
        <div className="receipt" style={{ marginTop: 6 }}>{error}</div>
      </div>
    );
  }
  if (phase === "empty") {
    return (
      <div className="plate rise" style={{ marginTop: 24, padding: "20px 24px" }}>
        <div style={{ fontSize: 14.5, fontWeight: 600 }}>No activity yet</div>
        <div style={{ marginTop: 6, color: "var(--text-muted)", fontSize: 13 }}>
          The graph grows as a byproduct of use — ask a few questions and the entities of
          co-retrieved documents will link themselves. No cron job will ever build this.
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="receipt" style={{ marginTop: 14 }}>
        {health && (
          <>top 50 of {health.entities.toLocaleString()} entities · {health.edges.toLocaleString()} live edges ·
            last grew {health.last_grown ?? "never"} · panel {Math.round(latency)}ms</>
        )}
      </div>

      <div className="plate rise" style={{ marginTop: 12, padding: 8, overflow: "hidden" }}>
        <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}>
          {lines.map((line, i) => (
            <line
              key={i}
              x1={line.x1} y1={line.y1} x2={line.x2} y2={line.y2}
              stroke="rgba(79, 216, 196, 0.14)"
              strokeWidth={Math.min(0.5 + line.weight * 0.35, 3)}
            />
          ))}
          {placed.map((node) => {
            const color = TYPE_COLOR[node.type ?? ""] ?? "var(--text-muted)";
            const active = node.id === selectedId;
            return (
              <g key={node.id} onClick={() => void select(node.id)} style={{ cursor: "pointer" }}>
                <circle cx={node.x} cy={node.y} r={node.r}
                        fill="rgba(13, 20, 38, 0.9)" stroke={color}
                        strokeWidth={active ? 2 : 1.1} />
                {node.r >= 9 && (
                  <text x={node.x} y={node.y + node.r + 11} textAnchor="middle"
                        fill={active ? "var(--text)" : "var(--text-muted)"}
                        style={{ fontSize: 10, fontFamily: "var(--mono)" }}>
                    {node.label.length > 18 ? `${node.label.slice(0, 17)}…` : node.label}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>

      {selected && (
        <div className="plate rise" style={{ marginTop: 12, padding: "18px 22px" }}>
          <div style={{ display: "flex", gap: 12, alignItems: "baseline", flexWrap: "wrap" }}>
            <span style={{ fontSize: 15, fontWeight: 650, letterSpacing: "-0.013em" }}>{selected.label}</span>
            <span className="receipt">{selected.type ?? "entity"} · confidence {selected.confidence}</span>
          </div>
          <div className="receipt" style={{ marginTop: 4, color: "var(--text-faint)" }}>
            from {selected.source_doc_id.split("/").pop()}
          </div>

          {selected.receipts.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className="receipt" style={{ color: "var(--conf-validated)" }}>receipts</div>
              {selected.receipts.map((receipt, i) => (
                <div key={i} className="receipt reveal" style={{ "--i": i } as React.CSSProperties}>
                  {receipt.claim_text.slice(0, 110)} · [{receipt.run_id.slice(0, 12)}
                  {receipt.retrieval_score != null ? ` · ${receipt.retrieval_score.toFixed(2)}` : ""}]
                </div>
              ))}
            </div>
          )}

          {selected.sessions.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div className="receipt" style={{ color: "var(--conf-validated)" }}>sessions that touched this</div>
              {selected.sessions.map((session) => (
                <div key={session.id} className="receipt">
                  {session.started_at?.slice(0, 16)} · {session.intent?.slice(0, 80)}
                </div>
              ))}
            </div>
          )}

          {selected.receipts.length === 0 && selected.sessions.length === 0 && (
            <div className="receipt" style={{ marginTop: 10, color: "var(--text-faint)" }}>
              seen in retrieval ({selected.runs.length} run(s)) — no answer has cited it yet
            </div>
          )}
        </div>
      )}
    </div>
  );
}
