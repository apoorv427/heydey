"use client";

// F4 — the graph, as an instrument you can hold.
//
// Was: a static picture (one force pass, then frozen; no drag, no zoom, no
// typed edges, anonymous grey lines). Now: a live simulation you can grab —
// drag pins a node where you drop it, wheel/pan/fit/reset move the view, two
// sliders spread the layout, clicking a node expands its audited 2-hop
// neighbourhood into the canvas and opens its full profile beside it.
//
// This component owns DATA + STATE; components/graph/GraphCanvas owns motion
// and hit-testing; components/graph/EntityProfile owns "everything about X".
// Four states, always: loading · empty-with-CTA · error-with-next-step · loaded.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { EntityProfile } from "./graph/EntityProfile";
import { GraphCanvas } from "./graph/GraphCanvas";
import {
  colorFor,
  edgeKey,
  typeLabel,
  type GraphEdge,
  type GraphNode,
  type Health,
  type NeighborRow,
  type Panel,
  type Relation,
} from "./graph/model";

type Phase = "loading" | "ready" | "empty" | "error";
type Selection = { id: number; label: string };

const DEFAULT_LINK_DISTANCE = 108;
const DEFAULT_CHARGE = -300;

const CONTROL: React.CSSProperties = {
  background: "rgba(10, 15, 30, 0.65)",
  border: "1px solid var(--plate-border)",
  borderRadius: 10,
  color: "var(--text)",
  fontSize: 12.5,
  outline: "none",
  padding: "7px 10px",
};

const ACTION: React.CSSProperties = {
  background: "rgba(79,216,196,.10)",
  border: "1px solid rgba(79,216,196,.35)",
  borderRadius: 10,
  color: "var(--conf-validated)",
  cursor: "pointer",
  fontSize: 12,
  padding: "7px 12px",
};

export function GraphPanel({ workspace = "blueleaf" }: { workspace?: string } = {}) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [error, setError] = useState("");
  const [graph, setGraph] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] }>({ nodes: [], edges: [] });
  const [health, setHealth] = useState<Health | null>(null);
  const [latency, setLatency] = useState(0);
  const [seeded, setSeeded] = useState(0);

  const [hiddenTypes, setHiddenTypes] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [linkDistance, setLinkDistance] = useState(DEFAULT_LINK_DISTANCE);
  const [charge, setCharge] = useState(DEFAULT_CHARGE);

  const [selected, setSelected] = useState<Selection | null>(null);
  const [expandedIds, setExpandedIds] = useState<number[]>([]);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [note, setNote] = useState("");
  // ids whose expansion is done or in flight — clicking the same node twice
  // must not re-fetch (and must not double-fetch under StrictMode)
  const requestedRef = useRef<Set<number>>(new Set());

  const load = useCallback(async () => {
    setPhase("loading");
    try {
      const response = await fetch(`/api/graph?workspace=${encodeURIComponent(workspace)}`);
      const body = (await response.json()) as Partial<Panel> & { detail?: string };
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      const panel = body as Panel;
      setHealth(panel.health);
      setLatency(panel.latency_ms ?? 0);
      setSeeded(panel.nodes.length);
      setSelected(null);
      setExpandedIds([]);
      requestedRef.current = new Set();
      setNote("");
      if (!panel.nodes.length) {
        setGraph({ nodes: [], edges: [] });
        setPhase("empty");
        return;
      }
      setGraph({
        nodes: panel.nodes.map((node) => ({ ...node, origin: "panel" as const })),
        edges: panel.edges.map((edge) => ({ ...edge, origin: "panel" as const })),
      });
      setPhase("ready");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setPhase("error");
    }
  }, [workspace]);

  useEffect(() => {
    void load();
  }, [load]);

  // ── expand: merge the audited 2-hop neighbourhood, never a full reload ─────
  const expand = useCallback(
    async (node: Selection, force = false) => {
      if (!force && requestedRef.current.has(node.id)) return;
      requestedRef.current.add(node.id);
      setBusyId(node.id);
      try {
        const response = await fetch(
          `/api/graph?neighbors=${encodeURIComponent(String(node.id))}&hops=2&limit=40` +
            `&workspace=${encodeURIComponent(workspace)}`,
        );
        const body = (await response.json()) as NeighborRow[] | { detail?: string };
        if (!response.ok || !Array.isArray(body)) {
          const detail = Array.isArray(body) ? "unexpected response" : body.detail ?? `HTTP ${response.status}`;
          requestedRef.current.delete(node.id); // a failed expand must stay retryable
          setNote(`could not expand ${node.label}: ${detail} — next: check the supervisor, then click again`);
          return;
        }
        let addedNodes = 0;
        let addedEdges = 0;
        setGraph((previous) => {
          const ids = new Set(previous.nodes.map((n) => n.id));
          const keys = new Set(previous.edges.map(edgeKey));
          const nodes = [...previous.nodes];
          const edges = [...previous.edges];
          for (const row of body) {
            if (!ids.has(row.id)) {
              ids.add(row.id);
              nodes.push({
                id: row.id,
                label: row.label,
                type: row.type,
                score: 0,
                origin: "expand",
                hop: row.hop,
              });
              addedNodes += 1;
            }
            if (row.via == null) continue;
            // weight stays null: the traversal does not report one, and the
            // canvas draws "unknown weight" rather than inventing a number
            const edge: GraphEdge = {
              source: row.via,
              target: row.id,
              predicate: row.predicate,
              weight: null,
              confidence: null,
              doc_id: row.doc_id,
              extractor: row.extractor,
              origin: "expand",
            };
            const key = edgeKey(edge);
            if (keys.has(key)) continue;
            keys.add(key);
            edges.push(edge);
            addedEdges += 1;
          }
          return { nodes, edges };
        });
        setExpandedIds((previous) => (previous.includes(node.id) ? previous : [...previous, node.id]));
        setNote(
          addedNodes || addedEdges
            ? `${node.label}: +${addedNodes} entities · +${addedEdges} links (2 hops, ${body.length} audited paths)`
            : `${node.label}: 2-hop neighbourhood already on the canvas (${body.length} paths, nothing new)`,
        );
      } catch (caught) {
        const detail = caught instanceof Error ? caught.message : String(caught);
        requestedRef.current.delete(node.id);
        setNote(`could not expand ${node.label}: ${detail} — next: check the supervisor, then click again`);
      } finally {
        setBusyId(null);
      }
    },
    [workspace],
  );

  const onSelect = useCallback(
    (node: GraphNode) => {
      setSelected({ id: node.id, label: node.label });
      void expand({ id: node.id, label: node.label });
    },
    [expand],
  );

  // walk from a relation row: the related entity joins the canvas carrying the
  // predicate and the doc it was asserted from
  const walk = useCallback((relation: Relation, fromId: number) => {
    const other = relation.entity;
    setGraph((previous) => {
      const nodes = previous.nodes.some((n) => n.id === other.id)
        ? previous.nodes
        : [...previous.nodes, { id: other.id, label: other.label, type: other.type, score: 0, origin: "expand" as const }];
      const edge: GraphEdge = {
        source: relation.direction === "out" ? fromId : other.id,
        target: relation.direction === "out" ? other.id : fromId,
        predicate: relation.predicate,
        weight: relation.weight,
        confidence: relation.confidence,
        doc_id: relation.doc_id,
        extractor: relation.extractor,
        origin: "expand",
      };
      const keys = new Set(previous.edges.map(edgeKey));
      const edges = keys.has(edgeKey(edge)) ? previous.edges : [...previous.edges, edge];
      return { nodes, edges };
    });
    setSelected({ id: other.id, label: other.label });
  }, []);

  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const node of graph.nodes) {
      const key = typeLabel(node.type);
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [graph.nodes]);

  const expandedCount = graph.nodes.length - seeded;

  // ── the four states ────────────────────────────────────────────────────────

  if (phase === "loading") {
    return (
      <div className="plate rise" style={{ marginTop: 20, padding: 18 }}>
        <div className="pulse" style={{ color: "var(--text-muted)", fontSize: 13.5 }}>growing the map…</div>
        <div className="pulse" style={{ marginTop: 12, height: 320, borderRadius: 12, background: "rgba(79,216,196,.045)" }} />
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="plate rise" style={{ marginTop: 20, padding: "18px 22px", borderColor: "rgba(232,161,60,.35)" }}>
        <div style={{ color: "var(--conf-warn)", fontSize: 13.5, fontWeight: 550 }}>Graph unavailable</div>
        <div className="receipt" style={{ marginTop: 6 }}>{error}</div>
        <div className="receipt" style={{ color: "var(--text-muted)" }}>
          next: start the supervisor — <code>api/.venv/bin/python api/heydey_supervisor.py</code> — then retry
        </div>
        <button type="button" style={{ ...ACTION, marginTop: 12 }} onClick={() => void load()}>retry</button>
      </div>
    );
  }

  if (phase === "empty") {
    return (
      <div className="plate rise" style={{ marginTop: 20, padding: "20px 24px" }}>
        <div style={{ fontSize: 14.5, fontWeight: 600 }}>No graph yet</div>
        <div style={{ marginTop: 6, color: "var(--text-muted)", fontSize: 13, maxWidth: 620 }}>
          The graph grows out of documents you ingest and questions you ask — never a scheduled
          crawl. Nothing has been indexed for <span style={{ fontFamily: "var(--mono)" }}>{workspace}</span> yet.
        </div>
        <div className="receipt" style={{ marginTop: 12, color: "var(--conf-validated)" }}>run an ingest</div>
        <div className="receipt">api/.venv/bin/python -m heydey.ops_ingest --workspace {workspace}</div>
        <div className="receipt">
          api/.venv/bin/python -m heydey.graph_backfill --workspace {workspace} --pass deterministic
        </div>
        <button type="button" style={{ ...ACTION, marginTop: 12 }} onClick={() => void load()}>check again</button>
      </div>
    );
  }

  return (
    <div>
      <div className="receipt" style={{ marginTop: 12, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        {health && (
          <span>
            top {seeded} of {health.entities.toLocaleString()} entities · {health.edges.toLocaleString()} live edges ·
            {" "}last grew {health.last_grown?.slice(0, 16).replace("T", " ") ?? "never"} · panel {Math.round(latency)}ms
            {expandedCount > 0 ? ` · +${expandedCount} expanded` : ""}
          </span>
        )}
        {health?.stalled_24h && (
          <span style={{ color: "var(--conf-warn)" }}>· no growth in 24h — next: ingest or ask something</span>
        )}
        <button
          type="button"
          onClick={() => void load()}
          style={{ ...ACTION, padding: "4px 9px", fontSize: 11 }}
          title="reload the ranked panel (clears expansions)"
        >
          reload
        </button>
      </div>

      <div className="plate" style={{ marginTop: 10, padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="search entities — centres the first match, dims the rest"
            aria-label="search entities"
            style={{ ...CONTROL, flex: "1 1 300px", minWidth: 220 }}
          />
          {search && (
            <button type="button" style={{ ...ACTION, padding: "6px 10px" }} onClick={() => setSearch("")}>clear</button>
          )}
          <label className="receipt" style={{ display: "flex", alignItems: "center", gap: 7 }}>
            link distance
            <input
              type="range"
              min={40}
              max={280}
              step={4}
              value={linkDistance}
              onChange={(event) => setLinkDistance(Number(event.target.value))}
              aria-label="link distance"
              style={{ width: 110 }}
            />
            <span style={{ color: "var(--text-faint)", width: 26 }}>{linkDistance}</span>
          </label>
          <label className="receipt" style={{ display: "flex", alignItems: "center", gap: 7 }}>
            repulsion
            <input
              type="range"
              min={-900}
              max={-40}
              step={10}
              value={charge}
              onChange={(event) => setCharge(Number(event.target.value))}
              aria-label="repulsion"
              style={{ width: 110 }}
            />
            <span style={{ color: "var(--text-faint)", width: 34 }}>{charge}</span>
          </label>
          <button
            type="button"
            style={{ ...ACTION, padding: "6px 10px" }}
            onClick={() => {
              setLinkDistance(DEFAULT_LINK_DISTANCE);
              setCharge(DEFAULT_CHARGE);
            }}
          >
            reset layout
          </button>
        </div>

        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <span className="receipt" style={{ color: "var(--text-faint)" }}>types:</span>
          {typeCounts.map(([type, count]) => {
            const off = hiddenTypes.includes(type);
            return (
              <button
                key={type}
                type="button"
                onClick={() =>
                  setHiddenTypes((previous) =>
                    previous.includes(type) ? previous.filter((t) => t !== type) : [...previous, type],
                  )
                }
                title={off ? `show ${type}` : `hide ${type}`}
                style={{
                  alignItems: "center",
                  background: off ? "transparent" : "rgba(255,255,255,.035)",
                  border: `1px solid ${off ? "var(--plate-border)" : colorFor(type === "untyped" ? null : type)}`,
                  borderRadius: 999,
                  color: off ? "var(--text-faint)" : "var(--text)",
                  cursor: "pointer",
                  display: "inline-flex",
                  fontFamily: "var(--mono)",
                  fontSize: 11,
                  gap: 6,
                  opacity: off ? 0.55 : 1,
                  padding: "4px 10px",
                }}
              >
                <span
                  style={{
                    background: colorFor(type === "untyped" ? null : type),
                    borderRadius: 999,
                    height: 7,
                    opacity: off ? 0.35 : 1,
                    width: 7,
                  }}
                />
                {type} {count}
              </button>
            );
          })}
          {hiddenTypes.length > 0 && (
            <button type="button" style={{ ...ACTION, padding: "4px 9px", fontSize: 11 }} onClick={() => setHiddenTypes([])}>
              show all
            </button>
          )}
        </div>
      </div>

      {note && (
        <div className="receipt" style={{ marginTop: 8, color: note.startsWith("could not") ? "var(--conf-warn)" : "var(--conf-validated)" }}>
          {note}
        </div>
      )}

      <div
        style={{
          display: "grid",
          gap: 12,
          gridTemplateColumns: selected ? "minmax(0, 1fr) minmax(300px, 360px)" : "minmax(0, 1fr)",
          marginTop: 10,
        }}
      >
        <div className="plate rise" style={{ padding: 6, overflow: "hidden" }}>
          <GraphCanvas
            nodes={graph.nodes}
            edges={graph.edges}
            hiddenTypes={hiddenTypes}
            search={search}
            selectedId={selected?.id ?? null}
            expandedIds={expandedIds}
            busyId={busyId}
            linkDistance={linkDistance}
            charge={charge}
            onSelect={onSelect}
          />
        </div>

        {selected && (
          <EntityProfile
            key={selected.id}
            entityId={selected.id}
            label={selected.label}
            workspace={workspace}
            onClose={() => setSelected(null)}
            onExpand={() => void expand(selected, true)}
            onWalk={(relation) => walk(relation, selected.id)}
          />
        )}
      </div>

      <div className="receipt" style={{ marginTop: 8, color: "var(--text-faint)" }}>
        node colour = entity type · node size = activity score · line thickness = co-retrieval weight ·
        dashed = added by a 2-hop expansion (no weight reported) · hover a line for its predicate and source doc
      </div>
    </div>
  );
}
