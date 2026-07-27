// Shared shapes + palette for the interactive graph surface.
//
// Every field here mirrors a field the supervisor actually returns
// (/graph, /graph/neighbors, /graph/profile) — the UI never invents a value.
// `weight: null` is deliberate: the 2-hop traversal does NOT return a weight,
// so an expanded edge renders as "unknown weight" rather than a made-up 1.0.

export type Origin = "panel" | "expand";

export type GraphNode = {
  id: number;
  label: string;
  type: string | null;
  score: number;
  confidence?: number | null;
  mentions?: number | null;
  /** where this node came into the canvas from — ranked panel, or an expansion */
  origin?: Origin;
  /** hops from the node that was expanded (expand-origin nodes only) */
  hop?: number | null;
};

export type GraphEdge = {
  source: number;
  target: number;
  predicate: string | null;
  /** null = the endpoint that produced this edge does not report a weight */
  weight: number | null;
  confidence?: number | null;
  doc_id?: string | null;
  last_seen?: string | null;
  sources?: number | null;
  extractor?: string | null;
  origin?: Origin;
};

export type Health = {
  entities: number;
  edges: number;
  by_extractor?: Record<string, number>;
  last_grown: string | null;
  stalled_24h?: boolean;
};

export type Panel = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  health: Health;
  latency_ms?: number;
};

/** One row of GET /graph/neighbors — a single audited hop. */
export type NeighborRow = {
  id: number;
  label: string;
  type: string | null;
  hop: number;
  predicate: string | null;
  doc_id: string | null;
  chunk_id: string | null;
  extractor: string | null;
  via: number | null;
};

export type Relation = {
  entity: { id: number; label: string; type: string | null };
  direction: "in" | "out";
  predicate: string | null;
  confidence: number | null;
  weight: number | null;
  doc_id: string | null;
  chunk_id: string | null;
  extractor: string | null;
};

export type Profile = {
  entity: {
    id: number;
    canonical_key: string;
    label: string;
    type: string | null;
    workspace_id: string | null;
    confidence: number | null;
    mention_count: number | null;
    first_seen: string | null;
    last_seen: string | null;
  };
  aliases: { alias: string; alias_key: string; source_doc_id: string | null; created_at: string }[];
  mention_docs: { doc_id: string; chunk_id: string | null; confidence: number | null; created_at: string }[];
  related: Relation[];
  receipts: {
    run_id: string;
    claim_text: string;
    chunk_id: string | null;
    retrieval_score: number | null;
    confidence_band: string | null;
    created_at: string;
  }[];
};

// ── palette (NOCTURNE: dark navy field, teal accents, cool instrument hues) ──

export const TYPE_COLOR: Record<string, string> = {
  product: "#4fd8c4",
  project: "#4fd8c4",
  org: "#6fcfa9",
  person: "#7fa8f0",
  technology: "#8fd3f4",
  decision: "#e8a13c",
  money: "#9cc487",
  date: "#7e8ba8",
  marker: "#a88fd8",
  lock: "#c9b45f",
  slice: "#5fbfd8",
  proper_noun: "#a8b2c8",
};

export const UNKNOWN_COLOR = "#a8b2c8";

export function colorFor(type: string | null | undefined): string {
  return TYPE_COLOR[type ?? ""] ?? UNKNOWN_COLOR;
}

export function typeLabel(type: string | null | undefined): string {
  return type && type.length ? type : "untyped";
}

/** Last path segment of a doc_id, with run:/hash ids left intact. */
export function docLabel(docId: string | null | undefined): string {
  if (!docId) return "no doc recorded";
  if (docId.startsWith("run:")) return docId;
  const tail = docId.split("/").pop() ?? docId;
  return tail.length > 46 ? `…${tail.slice(-45)}` : tail;
}

export function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** Stable key for an undirected (src,dst,predicate) edge, so merges dedupe. */
export function edgeKey(edge: GraphEdge): string {
  const a = Math.min(edge.source, edge.target);
  const b = Math.max(edge.source, edge.target);
  return `${a}-${b}-${edge.predicate ?? ""}`;
}
