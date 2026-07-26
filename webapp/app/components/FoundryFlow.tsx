"use client";

// F8 — Foundry / Architect onboarding flow (§D of the S6c contract).
// Five staggered scenes: Workspace -> Connect -> Interview -> Instantiate ->
// First answer + proof strip. Every rendered value comes from the API — the
// 5 interview questions from /foundry/status.interview (single source of
// truth, zero hardcoded question text) and the M:SS stopwatch numbers
// computed from real foundry_events timestamps (onboard_started ->
// fleet_instantiated -> first_answer). Four states honoured everywhere:
// loading / empty-with-CTA / error-with-next-step / loaded (+ the ingesting
// phase inside instantiate). NOCTURNE: reuses plate/reveal/chip/receipt/seal
// classes; confidence is light temperature, never red/green.

import { useCallback, useEffect, useMemo, useState } from "react";
import { ConnectorsPanel } from "./ConnectorsPanel";

// ── Interview payload shapes (mirrors foundry.py INTERVIEW; nothing invented) ──

type InterviewQuestion = {
  key: string;
  label: string;
  type: "choice" | "text" | "multi";
  options?: string[];
  options_by_playbook?: Record<string, string[]>;
  pattern?: string;
};

type FoundryEvent = {
  id: number;
  step: string;
  detail: string;
  created_at: string;
};

type ScanConnector = {
  connector_id: string;
  keychain_ref: string;
  scopes: string;
  results: number;
  flagged: number;
  last_sync: string | null;
  chunks: number;
};

type Scan = {
  chunks: number;
  docs: number;
  entities: number;
  connectors: ScanConnector[];
  clean_sources: string[];
};

type SpecRow = {
  id: string;
  name: string;
  version: number;
  spec_json: string;
  validator_pass: number;
  created_at: string;
  task_class?: string;
  k?: number;
  synthesize?: boolean;
  role?: string;
  focus?: string;
  playbook?: string;
};

type FoundryStatus = {
  workspace: string;
  phase: "empty" | "sources_ready" | "fleet_live";
  scan: Scan;
  specs: SpecRow[];
  events: FoundryEvent[];
  interview: InterviewQuestion[];
};

type Citation = {
  source: string;
  path: string;
  chunk: number;
  date: string;
  score: number | null;
  snippet: string;
  flagged: boolean;
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

// ── Small business-rule mapping (mirrors _PLAYBOOK_BY_BUSINESS_TYPE in foundry.py) ──
// The `sources` question already carries options_by_playbook keyed by playbook id;
// this table only maps business_type -> playbook id so the client can look those
// options up. No question text lives here — that all comes from /foundry/status.
const BUSINESS_TO_PLAYBOOK: Record<string, string> = {
  d2c: "d2c-ops",
  agency: "agency-brief",
};

// ── formatting helpers ────────────────────────────────────────────────────────

function formatMSS(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

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

// events are id DESC in the API — first match is the chronologically latest event.
function firstEvent(events: FoundryEvent[], step: string): FoundryEvent | undefined {
  return events.find((e) => e.step === step);
}

// ── Derive current onboard timing purely from event timestamps (§D stopwatch) ──

type Stopwatch = {
  fleetLiveMs: number | null;
  firstAnswerMs: number | null;
  startedAt: string | null;
};

function computeStopwatch(events: FoundryEvent[]): Stopwatch {
  const started = firstEvent(events, "onboard_started");
  const fleetInstantiated = firstEvent(events, "fleet_instantiated");
  const firstAnswerEvt = firstEvent(events, "first_answer");
  const t0 = started ? new Date(started.created_at).getTime() : null;
  // Guard: a stale fleet_instantiated older than the latest onboard_started must
  // not become "negative time to fleet". Compare event ids (monotonic in SQLite).
  const fleetLiveMs =
    t0 != null && fleetInstantiated && (!started || fleetInstantiated.id >= started.id)
      ? new Date(fleetInstantiated.created_at).getTime() - t0
      : null;
  const firstAnswerMs =
    t0 != null && firstAnswerEvt && (!started || firstAnswerEvt.id >= started.id)
      ? new Date(firstAnswerEvt.created_at).getTime() - t0
      : null;
  return {
    fleetLiveMs,
    firstAnswerMs,
    startedAt: started?.created_at ?? null,
  };
}

// ── The five-scene flow ───────────────────────────────────────────────────────

const HUMAN_LABEL: Record<string, string> = {
  d2c: "D2C brand",
  agency: "Creative agency",
  verbatim: "Verbatim (extractive)",
  synthesized: "Synthesized + cited",
  daily_brief: "Daily brief",
  cited_answers: "Cited answers",
  cost_watch: "Cost watch",
  client_briefs: "Client briefs",
  "demo-shopify": "Shopify (demo)",
  "demo-sheets": "Sheets (demo)",
  "demo-agency": "Agency intake (demo)",
};

function humanize(value: string): string {
  return HUMAN_LABEL[value] ?? value;
}

export function FoundryFlow() {
  // ── Scene 1: workspace ─────────────────────────────────────────────────────
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [wsDraft, setWsDraft] = useState("");
  const [wsBusy, setWsBusy] = useState(false);
  const [wsNote, setWsNote] = useState("");
  const [wsError, setWsError] = useState("");

  // ── Fetched status (interview text + scan + specs + events) ────────────────
  const [status, setStatus] = useState<FoundryStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [statusLoading, setStatusLoading] = useState(false);

  // ── Interview answers (state used across the reactive flow) ────────────────
  const [answers, setAnswers] = useState<Record<string, unknown>>({});

  // ── Onboard state ──────────────────────────────────────────────────────────
  const [onboarding, setOnboarding] = useState(false);
  const [onboardError, setOnboardError] = useState("");
  const [justOnboarded, setJustOnboarded] = useState(false);

  // ── Ask (Scene 5) state ────────────────────────────────────────────────────
  const [askQ, setAskQ] = useState("");
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState("");
  const [askResult, setAskResult] = useState<FullResult | null>(null);

  // ── Load status for the current workspace ──────────────────────────────────
  const loadStatus = useCallback(
    async (ws: string) => {
      setStatusLoading(true);
      try {
        const response = await fetch(`/api/foundry?workspace=${encodeURIComponent(ws)}`);
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
        setStatus(body as FoundryStatus);
        setStatusError("");
      } catch (caught) {
        setStatus(null);
        setStatusError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setStatusLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (workspace) void loadStatus(workspace);
  }, [workspace, loadStatus]);

  // ── Scene 1 — create workspace ─────────────────────────────────────────────
  async function createWorkspace() {
    const id = wsDraft.trim();
    if (!id) return;
    if (id === "blueleaf") {
      setWsError("blueleaf is the ops workspace — pick a fresh id for the Foundry demo");
      return;
    }
    setWsBusy(true);
    setWsError("");
    setWsNote("");
    try {
      const response = await fetch("/api/foundry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "create", id }),
      });
      const body = await response.json();
      if (response.ok) {
        setWorkspace(id);
        setWsNote(`workspace ${id} created`);
      } else if (response.status === 409) {
        // already exists — resume there instead of surfacing an error
        setWorkspace(id);
        setWsNote(`workspace ${id} exists — resuming`);
      } else {
        setWsError(body.detail ?? `HTTP ${response.status}`);
      }
    } catch (caught) {
      setWsError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setWsBusy(false);
    }
  }

  // ── Derived: playbook, filtered connectors, interview questions ────────────
  const businessType = typeof answers.business_type === "string" ? answers.business_type : "";
  const playbook = businessType ? BUSINESS_TO_PLAYBOOK[businessType] ?? "" : "";

  const interview = status?.interview ?? [];
  const sourcesQ = useMemo(() => interview.find((q) => q.key === "sources"), [interview]);
  const filteredServers = useMemo<string[] | undefined>(() => {
    if (!playbook || !sourcesQ) return undefined;
    return sourcesQ.options_by_playbook?.[playbook];
  }, [playbook, sourcesQ]);

  // ── Interview answer setters ───────────────────────────────────────────────
  function setChoice(key: string, value: string) {
    setAnswers((prev) => {
      const next = { ...prev, [key]: value };
      // Changing business_type invalidates playbook-derived answers.
      if (key === "business_type") {
        delete next.primary_goal;
        delete next.sources;
      }
      return next;
    });
  }

  function toggleMulti(key: string, value: string) {
    setAnswers((prev) => {
      const current = Array.isArray(prev[key]) ? [...(prev[key] as string[])] : [];
      const idx = current.indexOf(value);
      if (idx >= 0) current.splice(idx, 1);
      else current.push(value);
      return { ...prev, [key]: current };
    });
  }

  function setText(key: string, value: string) {
    setAnswers((prev) => ({ ...prev, [key]: value }));
  }

  // ── Interview completeness / validity check (per-question) ─────────────────
  const complete = useMemo(() => {
    if (interview.length === 0) return false;
    for (const q of interview) {
      const v = answers[q.key];
      if (q.type === "choice") {
        if (typeof v !== "string" || !v) return false;
        const options = q.options ?? (playbook ? q.options_by_playbook?.[playbook] : undefined);
        if (!options || !options.includes(v)) return false;
      } else if (q.type === "multi") {
        if (!Array.isArray(v) || v.length === 0) return false;
        const options = playbook ? q.options_by_playbook?.[playbook] ?? [] : [];
        for (const x of v as string[]) if (!options.includes(x)) return false;
      } else if (q.type === "text") {
        if (typeof v !== "string" || !v) return false;
        if (q.pattern && !new RegExp(q.pattern).test(v)) return false;
      }
    }
    return true;
  }, [answers, interview, playbook]);

  // ── Sources listed in Q4 must first appear in scan.clean_sources ───────────
  const cleanSources = status?.scan.clean_sources ?? [];
  const sourcesReady = useMemo(() => {
    const picked = Array.isArray(answers.sources) ? (answers.sources as string[]) : [];
    if (picked.length === 0) return false;
    return picked.every((s) => cleanSources.includes(s));
  }, [answers.sources, cleanSources]);

  // ── Scene 4 — instantiate the fleet ────────────────────────────────────────
  async function instantiate() {
    if (!workspace || !complete) return;
    setOnboarding(true);
    setOnboardError("");
    setJustOnboarded(false);
    try {
      const response = await fetch("/api/foundry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "onboard", workspace, answers }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setJustOnboarded(true);
      await loadStatus(workspace);
    } catch (caught) {
      setOnboardError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setOnboarding(false);
    }
  }

  // ── Scene 5 — ask, pinned to the analyst agent (spec[0]) ───────────────────
  async function ask() {
    if (!workspace || !status || status.specs.length === 0) return;
    const q = askQ.trim();
    if (!q) return;
    const analystId = status.specs[0].id;
    setAsking(true);
    setAskError("");
    setAskResult(null);
    try {
      const response = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: q,
          workspace,
          mode: "full",
          agent: analystId,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? `HTTP ${response.status}`);
      setAskResult(body as FullResult);
      await loadStatus(workspace);
    } catch (caught) {
      setAskError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setAsking(false);
    }
  }

  // ── Derived stopwatch (§D "fleet live in M:SS" + "first answer at M:SS") ──
  const stopwatch = useMemo<Stopwatch>(
    () => (status ? computeStopwatch(status.events) : { fleetLiveMs: null, firstAnswerMs: null, startedAt: null }),
    [status],
  );

  // ── UI ─────────────────────────────────────────────────────────────────────
  return (
    <div>
      {/* Header — the scene-setting receipt */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 14, color: "var(--text-muted)", lineHeight: 1.7, maxWidth: 640 }}>
          Foundry — Architect onboarding. Five deterministic questions map to a
          shelf of pre-authored specs; the fleet is a set of validated config
          rows, hydrated into the same pipeline every Ask uses. Zero LLM calls
          happen in this flow — the interview is a questionnaire.
        </div>
      </div>

      {/* ── Scene 1: Workspace ────────────────────────────────────────────── */}
      <Scene
        index={1}
        title="Workspace"
        subtitle={workspace ? `client · ${workspace}` : "onboard a new business — this workspace is one SQLite file, isolated by construction"}
      >
        {!workspace ? (
          <div>
            <div style={{ fontSize: 13.5, color: "var(--text-muted)", marginBottom: 12 }}>
              Pick an id for the new client workspace — anything but{" "}
              <span className="receipt" style={{ display: "inline" }}>blueleaf</span>{" "}
              (that's the ops corpus). A fresh id creates the workspace; an existing id resumes it.
            </div>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void createWorkspace();
              }}
              style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}
            >
              <input
                type="text"
                value={wsDraft}
                onChange={(event) => setWsDraft(event.target.value)}
                placeholder="e.g. demo-northstar"
                aria-label="workspace id"
                style={{
                  background: "transparent",
                  border: "1px solid var(--plate-border)",
                  borderRadius: 8,
                  padding: "8px 12px",
                  fontSize: 13,
                  fontFamily: "var(--mono)",
                  minWidth: 260,
                }}
              />
              <button
                type="submit"
                className="chip"
                disabled={wsBusy || !wsDraft.trim()}
                style={{
                  borderColor: "rgba(79,216,196,.4)",
                  color: "var(--conf-validated)",
                }}
              >
                {wsBusy ? "creating…" : "Onboard a business"}
              </button>
              {wsNote && (
                <span className="receipt seal" style={{ color: "var(--conf-validated)" }}>
                  {wsNote}
                </span>
              )}
              {wsError && (
                <span className="receipt" style={{ color: "var(--conf-warn)" }}>
                  {wsError}
                </span>
              )}
            </form>
          </div>
        ) : (
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <span className="receipt seal" style={{ color: "var(--conf-validated)" }}>
              workspace · {workspace}
            </span>
            <span className="receipt" style={{ color: "var(--text-faint)" }}>
              file boundary is the isolation mechanism — never a workspace_id filter
            </span>
            <button
              type="button"
              className="chip"
              style={{ padding: "3px 10px", fontSize: 11 }}
              onClick={() => {
                setWorkspace(null);
                setWsDraft("");
                setWsNote("");
                setAnswers({});
                setStatus(null);
                setAskResult(null);
                setAskQ("");
                setJustOnboarded(false);
              }}
            >
              change
            </button>
          </div>
        )}
      </Scene>

      {/* ── loading / status error ────────────────────────────────────────── */}
      {workspace && statusLoading && !status && (
        <div className="pulse" style={{ marginTop: 24, color: "var(--text-muted)", fontSize: 13.5 }}>
          loading foundry status…
        </div>
      )}
      {workspace && statusError && (
        <div
          className="plate rise"
          style={{ marginTop: 20, padding: "18px 22px", borderColor: "rgba(232,161,60,.35)" }}
        >
          <div style={{ color: "var(--conf-warn)", fontSize: 13.5, fontWeight: 550 }}>
            Foundry status unavailable
          </div>
          <div className="receipt" style={{ marginTop: 6 }}>{statusError}</div>
          <div style={{ marginTop: 10 }}>
            <button
              type="button"
              className="chip"
              onClick={() => void loadStatus(workspace)}
            >
              retry
            </button>
          </div>
        </div>
      )}

      {/* ── Scene 2: Connect ──────────────────────────────────────────────── */}
      {workspace && status && (
        <Scene
          index={2}
          title="Connect"
          subtitle={
            filteredServers
              ? `filtered to ${playbook} · ${filteredServers.length} source(s)`
              : "connect one or more source(s) — the injection guard screens every pull"
          }
        >
          {!businessType && (
            <div className="receipt" style={{ marginBottom: 10, color: "var(--text-muted)" }}>
              pick a business type in the interview below to filter this list to the playbook's connectors
            </div>
          )}
          <ConnectorsPanel workspace={workspace} servers={filteredServers}
            onSyncComplete={() => void loadStatus(workspace)} />
        </Scene>
      )}

      {/* ── Scene 3: Interview ────────────────────────────────────────────── */}
      {workspace && status && interview.length > 0 && (
        <Scene
          index={3}
          title="Interview"
          subtitle="5 questions · deterministic · no free text reaches a model"
        >
          <div>
            {interview.map((q, index) => (
              <div
                key={q.key}
                className="plate reveal"
                style={{
                  "--i": index,
                  padding: "16px 20px",
                  marginBottom: 10,
                } as React.CSSProperties}
              >
                <div style={{ fontSize: 14, fontWeight: 550, letterSpacing: "-0.013em" }}>
                  {q.label}
                </div>
                <div className="receipt" style={{ color: "var(--text-faint)", marginTop: 4 }}>
                  q{index + 1} · {q.key} · {q.type}
                  {q.pattern ? ` · pattern ${q.pattern}` : ""}
                </div>
                <div style={{ marginTop: 12 }}>
                  <QuestionControl
                    q={q}
                    playbook={playbook}
                    value={answers[q.key]}
                    onChoice={(v) => setChoice(q.key, v)}
                    onMulti={(v) => toggleMulti(q.key, v)}
                    onText={(v) => setText(q.key, v)}
                    cleanSources={cleanSources}
                  />
                </div>
              </div>
            ))}
            <div className="receipt" style={{ marginTop: 6, color: "var(--text-faint)" }}>
              5 questions · deterministic · no free text reaches a model
            </div>

            {/* Instantiate CTA */}
            <div style={{ marginTop: 18, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <button
                type="button"
                className="chip"
                disabled={!complete || !sourcesReady || onboarding}
                onClick={() => void instantiate()}
                style={
                  complete && sourcesReady
                    ? { borderColor: "rgba(79,216,196,.4)", color: "var(--conf-validated)" }
                    : undefined
                }
              >
                {onboarding ? "instantiating fleet…" : "Instantiate fleet"}
              </button>
              {!complete && (
                <span className="receipt" style={{ color: "var(--text-faint)" }}>
                  answer all 5 questions to enable
                </span>
              )}
              {complete && !sourcesReady && (
                <span className="receipt" style={{ color: "var(--conf-warn)" }}>
                  sync a source first — connect step (Scene 2)
                </span>
              )}
              {onboardError && (
                <span className="receipt" style={{ color: "var(--conf-warn)", whiteSpace: "pre-wrap" }}>
                  {onboardError}
                </span>
              )}
            </div>
          </div>
        </Scene>
      )}

      {/* ── Scene 4: Instantiate ──────────────────────────────────────────── */}
      {workspace && status && status.specs.length > 0 && (
        <Scene
          index={4}
          title="Instantiate"
          subtitle={`${status.specs.length} agent(s) validated · playbook ${status.specs[0].playbook ?? "?"}`}
        >
          {/* Scan roll-up plate */}
          <div className="plate" style={{ padding: "14px 18px", marginBottom: 12 }}>
            <div className="receipt" style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
              <span>
                chunks{" "}
                <span style={{ color: status.scan.chunks > 0 ? "var(--conf-validated)" : "var(--text-faint)" }}>
                  {status.scan.chunks}
                </span>
              </span>
              <span>
                docs <span style={{ color: "var(--text)" }}>{status.scan.docs}</span>
              </span>
              <span>
                entities <span style={{ color: "var(--text)" }}>{status.scan.entities}</span>
              </span>
              <span>
                connectors <span style={{ color: "var(--text)" }}>{status.scan.connectors.length}</span>
              </span>
              <span style={{ color: "var(--text-faint)" }}>
                clean_sources: {status.scan.clean_sources.length > 0 ? status.scan.clean_sources.join(", ") : "—"}
              </span>
            </div>
          </div>

          {/* Agent plates — landing 120ms apart when justOnboarded, instant on resume */}
          {status.specs.map((spec, index) => (
            <div
              key={spec.id}
              className={justOnboarded ? "plate reveal" : "plate rise"}
              style={
                justOnboarded
                  ? ({
                      padding: "14px 18px",
                      marginBottom: 10,
                      // Contract: agent plates land 120ms apart. reveal's default
                      // stagger is 60ms * --i; setting --i = index * 2 gives 120ms.
                      "--i": index * 2,
                    } as React.CSSProperties)
                  : { padding: "14px 18px", marginBottom: 10 }
              }
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "baseline",
                  flexWrap: "wrap",
                  gap: 8,
                }}
              >
                <span style={{ fontSize: 14.5, fontWeight: 600, letterSpacing: "-0.013em" }}>
                  {spec.name}
                </span>
                <span className="receipt" style={{ color: "var(--text-faint)" }}>
                  {spec.id} · v{spec.version}
                </span>
              </div>
              <div style={{ marginTop: 6, fontSize: 12.5, color: "var(--text-muted)", lineHeight: 1.6 }}>
                {spec.role ?? "—"}
              </div>
              <div className="receipt" style={{ marginTop: 8 }}>
                k={spec.k ?? "?"} · {spec.synthesize ? "synthesized" : "verbatim"}
                {spec.focus ? ` · focus: ${spec.focus}` : " · focus: —"} · playbook:{" "}
                {spec.playbook ?? "?"} ·{" "}
                <span
                  className="seal"
                  style={{ color: spec.validator_pass === 1 ? "var(--conf-validated)" : "var(--conf-warn)" }}
                >
                  {spec.validator_pass === 1 ? "validated ✓" : "not validated"}
                </span>
              </div>
            </div>
          ))}
        </Scene>
      )}

      {/* ── Scene 5: First answer + proof strip + stopwatch ───────────────── */}
      {workspace && status && status.specs.length > 0 && (
        <Scene
          index={5}
          title="First answer"
          subtitle={`ask the ${status.specs[0].name} — every sentence cited, cross-model validated`}
        >
          {/* the ask box, pinned to the analyst (spec[0]) */}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void ask();
            }}
            style={{
              background: "rgba(13, 20, 38, 0.72)",
              border: "1px solid var(--plate-border)",
              borderRadius: "var(--radius)",
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "6px 8px 6px 16px",
              marginBottom: 12,
            }}
          >
            <input
              className="slab-input"
              placeholder={`Ask ${status.specs[0].name}`}
              value={askQ}
              onChange={(event) => setAskQ(event.target.value)}
              aria-label="ask agent"
              style={{ padding: "10px 4px", fontSize: 15 }}
            />
            <button
              type="submit"
              className="chip"
              disabled={asking || !askQ.trim()}
              style={{
                borderColor: "rgba(79,216,196,.4)",
                color: "var(--conf-validated)",
                margin: 4,
              }}
            >
              {asking ? "asking…" : "Ask"}
            </button>
          </form>

          {asking && (
            <div className="pulse" style={{ color: "var(--text-muted)", fontSize: 13.5, marginBottom: 12 }}>
              retrieving evidence · cross-model validating…
            </div>
          )}
          {askError && (
            <div
              className="plate rise"
              style={{ padding: "14px 18px", borderColor: "rgba(232,161,60,.35)", marginBottom: 12 }}
            >
              <div style={{ color: "var(--conf-warn)", fontSize: 13.5, fontWeight: 550 }}>
                Couldn&apos;t complete the ask
              </div>
              <div className="receipt" style={{ marginTop: 6 }}>{askError}</div>
            </div>
          )}
          {askResult && <AskResult result={askResult} />}

          {/* Live Map summary strip — reconciles scan.connectors */}
          <div className="plate" style={{ padding: "14px 18px", marginTop: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 600, letterSpacing: "-0.013em", marginBottom: 8 }}>
              Live Map
            </div>
            {status.scan.connectors.length === 0 ? (
              <div className="receipt" style={{ color: "var(--text-faint)" }}>
                nothing connected yet
              </div>
            ) : (
              status.scan.connectors.map((c) => (
                <div key={c.connector_id} className="receipt" style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
                  <span style={{ color: "var(--text)" }}>{c.connector_id}</span>
                  <span>
                    results <span style={{ color: "var(--text)" }}>{c.results}</span>
                  </span>
                  <span style={{ color: c.flagged > 0 ? "var(--conf-warn)" : "var(--text-faint)" }}>
                    flagged {c.flagged}
                  </span>
                  <span>
                    corpus{" "}
                    <span style={{ color: c.chunks > 0 ? "var(--conf-validated)" : "var(--text-faint)" }}>
                      {c.chunks} chunk(s)
                    </span>
                  </span>
                </div>
              ))
            )}
          </div>

          {/* Isolation chip + stopwatch line (both computed from real state) */}
          <div style={{ marginTop: 14, display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <span
              className="chip"
              style={{
                borderColor: "rgba(79,216,196,.4)",
                color: "var(--conf-validated)",
                cursor: "default",
              }}
            >
              this workspace cannot see other clients
            </span>
            <span className="receipt" style={{ color: "var(--text-faint)" }}>
              file boundary · one SQLite per workspace · never a workspace_id filter as safety
            </span>
          </div>

          <div className="plate" style={{ marginTop: 14, padding: "14px 18px" }}>
            <div className="receipt" style={{ display: "flex", gap: 18, flexWrap: "wrap" }}>
              <span>
                fleet live in{" "}
                <span
                  style={{
                    color: stopwatch.fleetLiveMs != null ? "var(--conf-validated)" : "var(--text-faint)",
                    fontSize: 14,
                  }}
                >
                  {stopwatch.fleetLiveMs != null ? formatMSS(stopwatch.fleetLiveMs) : "—:—"}
                </span>
              </span>
              <span>
                first answer at{" "}
                <span
                  style={{
                    color: stopwatch.firstAnswerMs != null ? "var(--conf-validated)" : "var(--text-faint)",
                    fontSize: 14,
                  }}
                >
                  {stopwatch.firstAnswerMs != null ? formatMSS(stopwatch.firstAnswerMs) : "—:—"}
                </span>
              </span>
              <span style={{ color: "var(--text-faint)" }}>
                computed from foundry_events (onboard_started → fleet_instantiated → first_answer)
              </span>
            </div>
          </div>
        </Scene>
      )}
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

function Scene({
  index,
  title,
  subtitle,
  children,
}: {
  index: number;
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rise" style={{ marginTop: 24 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 12 }}>
        <span
          className="receipt"
          style={{
            display: "inline-block",
            padding: "3px 10px",
            border: "1px solid var(--plate-border)",
            borderRadius: 999,
            color: "var(--text-muted)",
          }}
        >
          {index}
        </span>
        <span style={{ fontSize: 17, fontWeight: 600, letterSpacing: "-0.013em" }}>{title}</span>
        {subtitle && (
          <span className="receipt" style={{ color: "var(--text-faint)" }}>
            · {subtitle}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

function QuestionControl({
  q,
  playbook,
  value,
  onChoice,
  onMulti,
  onText,
  cleanSources,
}: {
  q: InterviewQuestion;
  playbook: string;
  value: unknown;
  onChoice: (v: string) => void;
  onMulti: (v: string) => void;
  onText: (v: string) => void;
  cleanSources: string[];
}) {
  if (q.type === "choice") {
    const options = q.options ?? (playbook ? q.options_by_playbook?.[playbook] : undefined) ?? [];
    if (options.length === 0) {
      return (
        <div className="receipt" style={{ color: "var(--text-faint)" }}>
          pick a business type first — options depend on the playbook
        </div>
      );
    }
    return (
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {options.map((opt) => {
          const active = value === opt;
          return (
            <button
              key={opt}
              type="button"
              className="chip"
              onClick={() => onChoice(opt)}
              style={
                active
                  ? { borderColor: "rgba(79,216,196,.5)", color: "var(--conf-validated)" }
                  : undefined
              }
            >
              {humanize(opt)}
            </button>
          );
        })}
      </div>
    );
  }
  if (q.type === "multi") {
    const options = playbook ? q.options_by_playbook?.[playbook] ?? [] : [];
    const picked = Array.isArray(value) ? (value as string[]) : [];
    if (options.length === 0) {
      return (
        <div className="receipt" style={{ color: "var(--text-faint)" }}>
          pick a business type first — options depend on the playbook
        </div>
      );
    }
    return (
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {options.map((opt) => {
          const active = picked.includes(opt);
          const clean = cleanSources.includes(opt);
          return (
            <button
              key={opt}
              type="button"
              className="chip"
              onClick={() => onMulti(opt)}
              style={
                active
                  ? { borderColor: "rgba(79,216,196,.5)", color: "var(--conf-validated)" }
                  : undefined
              }
              title={clean ? "synced — chunks in corpus" : "not yet synced — Scene 2"}
            >
              {humanize(opt)}
              {active && !clean ? " · not synced yet" : ""}
            </button>
          );
        })}
      </div>
    );
  }
  // text
  const draft = typeof value === "string" ? value : "";
  const matches = q.pattern ? new RegExp(q.pattern).test(draft) : true;
  return (
    <div>
      <input
        type="text"
        value={draft}
        onChange={(event) => onText(event.target.value)}
        placeholder="short display name — appears in agent labels only"
        aria-label={q.label}
        style={{
          background: "transparent",
          border: "1px solid var(--plate-border)",
          borderRadius: 8,
          padding: "8px 12px",
          fontSize: 13.5,
          fontFamily: "var(--mono)",
          minWidth: 320,
          width: "100%",
          maxWidth: 420,
        }}
      />
      {draft && !matches && (
        <div className="receipt" style={{ marginTop: 6, color: "var(--conf-warn)" }}>
          fails pattern {q.pattern}
        </div>
      )}
      {draft && matches && (
        <div className="receipt" style={{ marginTop: 6, color: "var(--text-faint)" }}>
          ok · appears only in display strings, never in any prompt
        </div>
      )}
    </div>
  );
}

function AskResult({ result }: { result: FullResult }) {
  const sealTone =
    result.validator_pass && result.answer_kind === "synthesized"
      ? "var(--conf-validated)"
      : "var(--conf-warn)";
  const sealLabel =
    result.answer_kind === "synthesized"
      ? result.badge
      : `${result.badge} · ${result.answer_kind}`;
  return (
    <div className="plate rise" style={{ padding: "16px 20px" }}>
      <div style={{ fontSize: 15, lineHeight: 1.75, whiteSpace: "pre-wrap" }}>
        {result.answer}
      </div>
      <div style={{ marginTop: 14, borderTop: "1px solid var(--plate-border)", paddingTop: 10 }}>
        {result.citations.slice(0, 6).map((c, index) => (
          <div key={`${c.source}-${c.chunk}-${index}`} className="receipt reveal" style={{ "--i": index } as React.CSSProperties}>
            <span className={scoreClass(c.score)}>
              [KB: {basename(c.source)} · chunk {c.chunk}
              {c.date ? ` · ${c.date}` : ""}
              {c.score != null ? ` · score ${c.score.toFixed(3)}` : ""}]
            </span>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 14, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
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
          {result.duration_s.toFixed(1)}s · ${result.cost_usd.toFixed(4)} ·{" "}
          {result.retry_used ? "1 retry · " : ""}
          {result.ungrounded_count} ungrounded
        </span>
      </div>
    </div>
  );
}
