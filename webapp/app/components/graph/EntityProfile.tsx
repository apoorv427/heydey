"use client";

// "Everything about X" — the side panel that opens when a node is selected.
// Fed by GET /graph/profile (proxied through /api/graph?profile=). Every
// relation row carries its predicate AND the doc it was asserted from, because
// an untraceable relation is exactly the thing this product refuses to ship.
// Four states: loading · error-with-next-step · not-found-with-next-step · loaded.

import { useCallback, useEffect, useState } from "react";

import { colorFor, docLabel, typeLabel, truncate, type Profile, type Relation } from "./model";

type Props = {
  entityId: number;
  label: string;
  workspace: string;
  onClose: () => void;
  onExpand: () => void;
  /** walk a relation: the other entity joins the canvas carrying this edge's provenance */
  onWalk: (relation: Relation) => void;
};

type Phase = "loading" | "ready" | "missing" | "error";

const SECTION: React.CSSProperties = { marginTop: 14 };
const HEADING: React.CSSProperties = { color: "var(--conf-validated)", letterSpacing: "0.04em" };

export function EntityProfile({ entityId, label, workspace, onClose, onExpand, onWalk }: Props) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [profile, setProfile] = useState<Profile | null>(null);
  const [detail, setDetail] = useState("");
  const [nextStep, setNextStep] = useState("");

  const load = useCallback(async () => {
    setPhase("loading");
    try {
      const response = await fetch(
        `/api/graph?profile=${encodeURIComponent(String(entityId))}` +
          `&label=${encodeURIComponent(label)}&workspace=${encodeURIComponent(workspace)}`,
      );
      const body = (await response.json()) as Partial<Profile> & { detail?: string; next_step?: string };
      if (response.status === 404) {
        setDetail(body.detail ?? `no profile for ${label}`);
        setNextStep(body.next_step ?? "run the graph backfill, or try the canonical label");
        setPhase("missing");
        return;
      }
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setProfile(body as Profile);
      setPhase("ready");
    } catch (caught) {
      setDetail(caught instanceof Error ? caught.message : String(caught));
      setNextStep("check the supervisor is up, then retry");
      setPhase("error");
    }
  }, [entityId, label, workspace]);

  useEffect(() => {
    void load();
  }, [load]);

  const header = (
    <div style={{ display: "flex", alignItems: "baseline", gap: 10, justifyContent: "space-between" }}>
      <span style={{ fontSize: 15.5, fontWeight: 650, letterSpacing: "-0.013em" }}>{label}</span>
      <button
        type="button"
        onClick={onClose}
        aria-label="close entity profile"
        style={{ background: "none", border: "none", color: "var(--text-faint)", cursor: "pointer", fontSize: 15 }}
      >
        ×
      </button>
    </div>
  );

  if (phase === "loading") {
    return (
      <div className="plate" style={{ padding: "16px 18px" }}>
        {header}
        <div className="pulse receipt" style={{ marginTop: 10 }}>reading the profile…</div>
      </div>
    );
  }

  if (phase === "error" || phase === "missing") {
    return (
      <div className="plate" style={{ padding: "16px 18px", borderColor: "rgba(232,161,60,.35)" }}>
        {header}
        <div className="receipt" style={{ marginTop: 8, color: "var(--conf-warn)" }}>{detail}</div>
        <div className="receipt" style={{ color: "var(--text-muted)" }}>next: {nextStep}</div>
        <button
          type="button"
          onClick={() => void load()}
          style={{
            marginTop: 10, background: "rgba(79,216,196,.10)", border: "1px solid rgba(79,216,196,.35)",
            borderRadius: 9, color: "var(--conf-validated)", cursor: "pointer", fontSize: 12, padding: "6px 11px",
          }}
        >
          retry
        </button>
      </div>
    );
  }

  const data = profile as Profile;
  const entity = data.entity;
  const color = colorFor(entity.type);

  return (
    <div className="plate" style={{ padding: "16px 18px", maxHeight: "min(66vh, 620px)", overflowY: "auto" }}>
      {header}
      <div className="receipt" style={{ marginTop: 2 }}>
        <span style={{ color }}>{typeLabel(entity.type)}</span>
        {" · "}confidence {entity.confidence ?? "unknown"}
        {" · "}{entity.mention_count ?? 0} mentions
      </div>
      <div className="receipt" style={{ color: "var(--text-faint)" }}>
        key {entity.canonical_key} · id {entity.id}
        {entity.first_seen ? ` · first seen ${entity.first_seen.slice(0, 10)}` : ""}
        {entity.last_seen ? ` · last ${entity.last_seen.slice(0, 10)}` : ""}
      </div>

      <button
        type="button"
        onClick={onExpand}
        style={{
          marginTop: 10, background: "rgba(79,216,196,.10)", border: "1px solid rgba(79,216,196,.35)",
          borderRadius: 9, color: "var(--conf-validated)", cursor: "pointer", fontSize: 12, padding: "6px 11px",
        }}
      >
        expand 2 hops on the canvas
      </button>

      <div style={SECTION}>
        <div className="receipt" style={HEADING}>aliases ({data.aliases.length})</div>
        {data.aliases.length === 0 && (
          <div className="receipt" style={{ color: "var(--text-faint)" }}>none recorded</div>
        )}
        {data.aliases.slice(0, 8).map((alias) => (
          <div key={alias.alias_key} className="receipt">
            {alias.alias} <span style={{ color: "var(--text-faint)" }}>· {docLabel(alias.source_doc_id)}</span>
          </div>
        ))}
      </div>

      <div style={SECTION}>
        <div className="receipt" style={HEADING}>relations ({data.related.length})</div>
        {data.related.length === 0 && (
          <div className="receipt" style={{ color: "var(--text-faint)" }}>
            no typed relation yet — next: run the llm backfill pass over this doc
          </div>
        )}
        {data.related.slice(0, 25).map((relation, index) => (
          <div key={`${relation.entity.id}-${relation.predicate}-${index}`} className="receipt">
            <button
              type="button"
              onClick={() => onWalk(relation)}
              title="add this entity to the canvas and select it"
              style={{
                background: "none", border: "none", cursor: "pointer", padding: 0,
                color: colorFor(relation.entity.type), fontFamily: "var(--mono)", fontSize: 11,
              }}
            >
              {relation.direction === "out" ? "→" : "←"} {truncate(relation.entity.label, 28)}
            </button>
            <span style={{ color: "var(--text-muted)" }}> · {relation.predicate ?? "RELATED"}</span>
            <span style={{ color: "var(--text-faint)" }}>
              {" · "}{docLabel(relation.doc_id)}
              {relation.confidence != null ? ` · conf ${relation.confidence}` : ""}
              {relation.extractor ? ` · ${relation.extractor}` : ""}
            </span>
          </div>
        ))}
      </div>

      <div style={SECTION}>
        <div className="receipt" style={HEADING}>mentioned in ({data.mention_docs.length} docs)</div>
        {data.mention_docs.length === 0 && (
          <div className="receipt" style={{ color: "var(--text-faint)" }}>no mention rows</div>
        )}
        {data.mention_docs.slice(0, 10).map((mention, index) => (
          <div key={`${mention.doc_id}-${index}`} className="receipt" title={mention.doc_id}>
            {docLabel(mention.doc_id)}
            {mention.chunk_id ? <span style={{ color: "var(--text-faint)" }}> · {mention.chunk_id}</span> : null}
          </div>
        ))}
        {data.mention_docs.length > 10 && (
          <div className="receipt" style={{ color: "var(--text-faint)" }}>
            +{data.mention_docs.length - 10} more
          </div>
        )}
      </div>

      <div style={SECTION}>
        <div className="receipt" style={HEADING}>receipts ({data.receipts.length})</div>
        {data.receipts.length === 0 && (
          <div className="receipt" style={{ color: "var(--text-faint)" }}>
            no answer has cited this entity yet
          </div>
        )}
        {data.receipts.map((receipt, index) => (
          <div key={`${receipt.run_id}-${index}`} className="receipt reveal" style={{ "--i": index } as React.CSSProperties}>
            {truncate(receipt.claim_text, 120)}
            <span style={{ color: "var(--text-faint)" }}>
              {" ["}{receipt.run_id.slice(0, 12)}
              {receipt.retrieval_score != null ? ` · ${receipt.retrieval_score.toFixed(2)}` : ""}
              {receipt.confidence_band ? ` · ${receipt.confidence_band}` : ""}
              {"]"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
