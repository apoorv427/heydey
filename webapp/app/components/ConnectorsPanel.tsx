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
//
// Builder-C pass (founder review): the demo servers above are SYNTHETIC —
// labeled as such everywhere, and a sync that gets routed away from a
// protected real workspace now reads as a plain sentence, never an error.
// Below them, a real OAuth connect flow for Google Workspace: a plain-language
// setup card (no OAuth jargon assumed), Connect/Disconnect, granted scopes,
// and token expiry. `realAccounts` defaults to false so the /agents Foundry
// embed (which passes its own `servers` filter and expects the pre-existing
// demo-only picker) renders exactly as before — only /connectors/page.tsx
// opts in.

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

// A sync that landed real rows vs. one the server deliberately routed away
// from a protected workspace (a SYNTHETIC connector may never touch a real
// corpus — see connector_sync._assert_sync_allowed). Both are SUCCESS
// outcomes; neither is an error.
type SyncOutcome =
  | { kind: "report"; report: SyncReport }
  | { kind: "routed"; note: string; routedTo?: string };

type ConnectorsData = { connectors: LiveRow[]; known: string[] };

type OAuthStatus = {
  configured: boolean;
  connected: boolean;
  scopes?: string[] | string | null;
  expires_at?: string | number | null;
  redirect_uri?: string | null;
};

// Human labels for the known local (synthetic) servers; unknown ids fall back
// to a generated "<name> (demo)" label so a new demo server never renders as
// a bare machine id.
const SERVER_LABEL: Record<string, string> = {
  "demo-shopify": "Shopify (demo store)",
  "demo-sheets": "Google Sheets (demo)",
  "demo-agency": "Agency workspace (demo)",
};

const SERVER_NOTE: Record<string, string> = {
  "demo-shopify": "orders · line items · RTO — synthetic, PII-free (§14-C5)",
  "demo-sheets": "ad-spend rows · channel CAC — synthetic, PII-free (§14-C5)",
  "demo-agency": "agency-side rows — synthetic, PII-free (§14-C5)",
};

function demoLabel(id: string): string {
  if (SERVER_LABEL[id]) return SERVER_LABEL[id];
  if (id.startsWith("demo-")) {
    const rest = id.slice(5).replace(/-/g, " ");
    return `${rest.charAt(0).toUpperCase()}${rest.slice(1)} (demo)`;
  }
  return id;
}

function shortDate(iso: string | null | undefined): string {
  if (!iso) return "never";
  return iso.length >= 16 ? iso.slice(0, 16).replace("T", " ") : iso;
}

function formatExpiry(value: string | number | null | undefined): string {
  if (value == null || value === "") return "";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isNaN(num) && typeof value !== "object") {
    try {
      return new Date(num * 1000).toISOString().slice(0, 16).replace("T", " ");
    } catch {
      // fall through to string handling below
    }
  }
  return shortDate(String(value));
}

// The one place a failure becomes a sentence. Prefers the API's own
// next_step; refuses to surface the raw "HTTP xxx / non-JSON body" shape —
// that exact string is the bug the founder hit on Sync now.
function describeError(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    if (typeof record.next_step === "string" && record.next_step.trim()) {
      return record.next_step;
    }
    if (typeof record.detail === "string" && record.detail.trim()) {
      const looksRaw = /non-JSON body|^HTTP \d|Internal Server Error/i.test(record.detail);
      if (!looksRaw) return record.detail;
    }
  }
  return fallback;
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  background: "var(--field)",
  border: "1px solid var(--plate-border)",
  borderRadius: 10,
  padding: "9px 11px",
  fontSize: 13,
  color: "var(--text)",
  outline: "none",
  marginTop: 4,
};

const labelStyle: React.CSSProperties = { fontSize: 12, color: "var(--text-faint)", display: "block" };

// ── Google Workspace: real OAuth connect flow ─────────────────────────────

const GOOGLE_CONNECTOR_ID = "google-workspace";

// Static display copy from heydey/manifests/google-workspace.json (title +
// scopes) — live status (configured/connected/expiry/redirect_uri) always
// comes from the API, never invented here.
const GOOGLE_SCOPE_LABEL: Record<string, string> = {
  "https://www.googleapis.com/auth/drive.readonly": "Google Drive — read files",
  "https://www.googleapis.com/auth/gmail.readonly": "Gmail — read messages",
  "https://www.googleapis.com/auth/calendar.readonly": "Calendar — read events",
};

function scopesToList(raw: OAuthStatus["scopes"]): string[] {
  if (Array.isArray(raw)) return raw.filter(Boolean);
  if (typeof raw === "string") return raw.split(/\s+/).filter(Boolean);
  return [];
}

type CredentialsFormProps = {
  workspace: string;
  onSaved: () => unknown;
};

function GoogleCredentialsForm({ workspace, onSaved }: CredentialsFormProps) {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!clientId.trim()) {
      setFormError("Paste the Client ID Google showed you first.");
      return;
    }
    setSaving(true);
    setFormError("");
    try {
      const response = await fetch("/api/oauth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "config",
          connector_id: GOOGLE_CONNECTOR_ID,
          workspace,
          client_id: clientId.trim(),
          client_secret: clientSecret.trim() || undefined,
        }),
      });
      const body = await response.json();
      if (!response.ok) {
        throw new Error(describeError(body, `couldn't save those credentials (HTTP ${response.status})`));
      }
      // Never keep the secret in memory/UI longer than it takes to save it.
      setClientId("");
      setClientSecret("");
      await onSaved();
    } catch (caught) {
      setFormError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={submit} style={{ marginTop: 14, display: "grid", gap: 10, maxWidth: 420 }}>
      <label style={labelStyle}>
        Client ID
        <input
          style={inputStyle}
          value={clientId}
          onChange={(event) => setClientId(event.target.value)}
          placeholder="123456-abc.apps.googleusercontent.com"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <label style={labelStyle}>
        Client secret
        <input
          type="password"
          style={inputStyle}
          value={clientSecret}
          onChange={(event) => setClientSecret(event.target.value)}
          placeholder="paste the secret Google showed you"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      {formError && <div className="receipt" style={{ color: "var(--conf-warn)" }}>{formError}</div>}
      <button
        type="submit"
        className="chip"
        disabled={saving}
        style={{ justifySelf: "start", borderColor: "rgba(79,216,196,.4)", color: "var(--conf-validated)" }}
      >
        {saving ? <span className="pulse">saving…</span> : "Save credentials"}
      </button>
      <div className="receipt" style={{ color: "var(--text-faint)" }}>
        Stored locally on this machine. The secret is never shown back to you after this.
      </div>
    </form>
  );
}

function GoogleConnectCard({ workspace }: { workspace: string }) {
  const [status, setStatus] = useState<OAuthStatus | null>(null);
  const [phase, setPhase] = useState<"loading" | "loaded" | "error">("loading");
  const [errorMsg, setErrorMsg] = useState("");
  const [busy, setBusy] = useState<"starting" | "syncing" | "disconnecting" | null>(null);
  const [editingCreds, setEditingCreds] = useState(false);
  const [waitingForConsent, setWaitingForConsent] = useState(false);
  const [consentFallbackUrl, setConsentFallbackUrl] = useState("");
  const [copyHint, setCopyHint] = useState("");
  const [syncNote, setSyncNote] = useState("");

  const load = useCallback(async () => {
    try {
      const response = await fetch(
        `/api/oauth?connector_id=${GOOGLE_CONNECTOR_ID}&workspace=${encodeURIComponent(workspace)}`,
      );
      const body = await response.json();
      if (!response.ok) {
        if (response.status === 404) {
          throw new Error(
            "the Google connector endpoints aren't available in this build yet — update and reload.",
          );
        }
        throw new Error(describeError(body, `couldn't check the connection (HTTP ${response.status})`));
      }
      setStatus(body as OAuthStatus);
      setPhase("loaded");
      setErrorMsg("");
      return body as OAuthStatus;
    } catch (caught) {
      setErrorMsg(caught instanceof Error ? caught.message : String(caught));
      setPhase("error");
      return null;
    }
  }, [workspace]);

  useEffect(() => {
    void load();
  }, [load]);

  // While waiting for the founder to approve access in the Google tab, poll
  // status every 3s (cap ~90s) so "Connected" appears on its own.
  useEffect(() => {
    if (!waitingForConsent) return;
    let stopped = false;
    let attempts = 0;
    const timer = setInterval(async () => {
      if (stopped) return;
      attempts += 1;
      const fresh = await load();
      if (fresh?.connected) {
        stopped = true;
        setWaitingForConsent(false);
        clearInterval(timer);
        return;
      }
      if (attempts >= 30) {
        stopped = true;
        setWaitingForConsent(false);
        clearInterval(timer);
      }
    }, 3000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [waitingForConsent, load]);

  async function startConnect() {
    setBusy("starting");
    setErrorMsg("");
    setConsentFallbackUrl("");
    try {
      const response = await fetch("/api/oauth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "start", connector_id: GOOGLE_CONNECTOR_ID, workspace }),
      });
      const body = await response.json();
      if (!response.ok) {
        throw new Error(describeError(body, `couldn't start Google sign-in (HTTP ${response.status})`));
      }
      const consentUrl = (body as { consent_url?: string }).consent_url ?? "";
      if (!consentUrl) {
        throw new Error("Google sign-in didn't return a consent link — try again in a moment.");
      }
      const win = window.open(consentUrl, "_blank", "noopener,noreferrer");
      if (!win) setConsentFallbackUrl(consentUrl); // popup blocked — offer a manual link
      setWaitingForConsent(true);
    } catch (caught) {
      setErrorMsg(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  async function disconnect() {
    setBusy("disconnecting");
    setErrorMsg("");
    try {
      const response = await fetch("/api/oauth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "disconnect", connector_id: GOOGLE_CONNECTOR_ID, workspace }),
      });
      const body = await response.json();
      if (!response.ok) {
        throw new Error(describeError(body, `couldn't disconnect (HTTP ${response.status})`));
      }
      await load();
    } catch (caught) {
      setErrorMsg(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  async function syncNow() {
    setBusy("syncing");
    setErrorMsg("");
    setSyncNote("");
    try {
      const response = await fetch("/api/connectors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "sync", workspace, connector_id: GOOGLE_CONNECTOR_ID }),
      });
      const body = await response.json();
      if (!response.ok) {
        const record = body as Record<string, unknown>;
        if (typeof record.detail === "string" && /unknown connector/i.test(record.detail)) {
          // Honest, not hidden: v1's manifest is auth-only (see google-workspace.json
          // "notes") — the account is connected and the token is stored; pulling
          // Drive/Gmail/Calendar content into the corpus is the next build slice.
          throw new Error(
            "Google is connected and the token is stored safely — pulling Drive, Gmail, and " +
              "Calendar content into the corpus isn't wired up yet in this build.",
          );
        }
        throw new Error(describeError(body, `sync didn't complete (HTTP ${response.status})`));
      }
      if (body && typeof body === "object" && "note" in (body as Record<string, unknown>)) {
        setSyncNote(String((body as Record<string, unknown>).note ?? "synced."));
      } else if (body && typeof body === "object" && "chunks" in (body as Record<string, unknown>)) {
        const report = body as SyncReport;
        setSyncNote(`pulled ${report.tools_pulled} tool(s) · ${report.chunks} chunk(s) · flagged ${report.flagged}`);
      } else {
        setSyncNote("sync completed.");
      }
    } catch (caught) {
      setErrorMsg(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  function copyRedirectUri() {
    if (!status?.redirect_uri) return;
    navigator.clipboard?.writeText(status.redirect_uri).then(
      () => {
        setCopyHint("copied");
        setTimeout(() => setCopyHint(""), 1500);
      },
      () => setCopyHint(""),
    );
  }

  const scopeList = useMemo(() => scopesToList(status?.scopes), [status]);

  return (
    <div className="plate rise" style={{ padding: "18px 22px", marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
        <span style={{ fontSize: 14.5, fontWeight: 600, letterSpacing: "-0.013em" }}>Google Workspace</span>
        <span className="receipt">real account · Drive · Gmail · Calendar</span>
      </div>

      {phase === "loading" && (
        <div className="pulse" style={{ marginTop: 12, color: "var(--text-muted)", fontSize: 13 }}>
          checking connection…
        </div>
      )}

      {phase === "error" && (
        <div style={{ marginTop: 12 }}>
          <div style={{ color: "var(--conf-warn)", fontSize: 13, fontWeight: 550 }}>Can&apos;t check status right now</div>
          <div className="receipt" style={{ marginTop: 4 }}>{errorMsg}</div>
          <button
            type="button"
            className="chip"
            style={{ marginTop: 10 }}
            onClick={() => {
              setPhase("loading");
              void load();
            }}
          >
            Retry
          </button>
        </div>
      )}

      {phase === "loaded" && status && !status.configured && (
        <div style={{ marginTop: 14 }}>
          <div style={{ color: "var(--text-muted)", fontSize: 13, lineHeight: 1.6 }}>
            To connect a real Google account, Google first asks you to create a free credential —
            think of it as a name tag that tells Google &quot;this is Heydey, running on your own
            computer, asking on your behalf.&quot; It takes about five minutes; the keys you create
            stay on this machine and Heydey never sends them anywhere else.
          </div>
          <ol style={{ marginTop: 10, paddingLeft: 20, color: "var(--text-muted)", fontSize: 12.5, lineHeight: 1.9 }}>
            <li>
              Open{" "}
              <a
                href="https://console.cloud.google.com/apis/credentials"
                target="_blank"
                rel="noreferrer"
                style={{ textDecoration: "underline", color: "var(--text)" }}
              >
                Google Cloud Console → Credentials ↗
              </a>{" "}
              and sign in with the Google account you want to connect.
            </li>
            <li>Create a project if you don&apos;t have one yet (any name is fine).</li>
            <li>
              Under &quot;OAuth consent screen&quot;, choose <b>External</b>, fill in the required
              fields, and add your own email as a test user.
            </li>
            <li>
              Back on <b>Credentials</b>, click <b>Create Credentials → OAuth client ID</b>, and pick{" "}
              <b>Desktop app</b> as the application type.
            </li>
            <li>If Google asks for a redirect URI, paste the one below exactly.</li>
            <li>Google will show you a <b>Client ID</b> and <b>Client secret</b> — copy both and paste them here.</li>
          </ol>

          <div style={{ marginTop: 12 }}>
            <div className="receipt" style={{ marginBottom: 4 }}>redirect URI (paste exactly if asked)</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <code
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 11.5,
                  color: "var(--text)",
                  background: "var(--field)",
                  border: "1px solid var(--plate-border)",
                  borderRadius: 8,
                  padding: "6px 10px",
                  wordBreak: "break-all",
                }}
              >
                {status.redirect_uri || "unavailable — the supervisor didn't report one yet"}
              </code>
              {status.redirect_uri && (
                <button type="button" className="chip" onClick={copyRedirectUri}>
                  {copyHint || "copy"}
                </button>
              )}
            </div>
          </div>

          <GoogleCredentialsForm workspace={workspace} onSaved={load} />
        </div>
      )}

      {phase === "loaded" && status && status.configured && !status.connected && (
        <div style={{ marginTop: 14 }}>
          <div style={{ color: "var(--text-muted)", fontSize: 13, lineHeight: 1.6 }}>
            Credentials are saved. Connect your Google account so Heydey can read (never write)
            the Drive files, Gmail messages, and Calendar events you approve.
          </div>
          <div style={{ display: "flex", gap: 10, marginTop: 12, alignItems: "center", flexWrap: "wrap" }}>
            <button
              type="button"
              className="chip"
              disabled={busy != null}
              style={{ borderColor: "rgba(79,216,196,.4)", color: "var(--conf-validated)" }}
              onClick={() => void startConnect()}
            >
              {busy === "starting" ? <span className="pulse">opening Google sign-in…</span> : "Connect Google account"}
            </button>
            {waitingForConsent && (
              <>
                <span className="receipt pulse">waiting for you to approve access in the Google tab…</span>
                <button type="button" className="chip" onClick={() => void load()}>
                  I approved — check now
                </button>
              </>
            )}
          </div>
          {consentFallbackUrl && (
            <div className="receipt" style={{ marginTop: 8 }}>
              Your browser blocked the popup —{" "}
              <a
                href={consentFallbackUrl}
                target="_blank"
                rel="noreferrer"
                style={{ textDecoration: "underline", color: "var(--text)" }}
              >
                open Google sign-in manually ↗
              </a>
            </div>
          )}
          {errorMsg && (
            <div className="receipt" style={{ marginTop: 8, color: "var(--conf-warn)" }}>{errorMsg}</div>
          )}
          <div style={{ marginTop: 12 }}>
            <button type="button" className="chip" style={{ fontSize: 11.5 }} onClick={() => setEditingCreds((v) => !v)}>
              {editingCreds ? "hide" : "change client ID / secret"}
            </button>
            {editingCreds && <GoogleCredentialsForm workspace={workspace} onSaved={load} />}
          </div>
        </div>
      )}

      {phase === "loaded" && status && status.configured && status.connected && (
        <div style={{ marginTop: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span
              style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--conf-validated)", display: "inline-block" }}
            />
            <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--conf-validated)" }}>Connected</span>
            <span className="receipt">
              {status.expires_at ? `token expires ${formatExpiry(status.expires_at)} · refreshes automatically` : "token refreshes automatically"}
            </span>
          </div>

          <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 8 }}>
            {scopeList.length > 0 ? (
              scopeList.map((scope) => (
                <span
                  key={scope}
                  className="receipt"
                  style={{ padding: "4px 10px", border: "1px solid var(--plate-border)", borderRadius: 999 }}
                >
                  {GOOGLE_SCOPE_LABEL[scope] ?? scope}
                </span>
              ))
            ) : (
              <span className="receipt" style={{ color: "var(--text-faint)" }}>no scopes reported</span>
            )}
          </div>

          <div style={{ display: "flex", gap: 10, marginTop: 14, alignItems: "center", flexWrap: "wrap" }}>
            <button type="button" className="chip" disabled={busy != null} onClick={() => void syncNow()}>
              {busy === "syncing" ? <span className="pulse">syncing…</span> : "Sync now"}
            </button>
            <button
              type="button"
              className="chip"
              disabled={busy != null}
              style={{ color: "var(--conf-warn)" }}
              onClick={() => void disconnect()}
            >
              {busy === "disconnecting" ? <span className="pulse">disconnecting…</span> : "Disconnect"}
            </button>
            {syncNote && <span className="receipt seal">{syncNote}</span>}
          </div>
          {errorMsg && (
            <div className="receipt" style={{ marginTop: 8, color: "var(--conf-warn)" }}>{errorMsg}</div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Demo connectors + Live Map (existing behaviour, error copy hardened) ──

type ConnectorsPanelProps = {
  workspace?: string;
  servers?: string[];
  // Fires after a successful sync/register so an embedding surface (e.g. Foundry)
  // can refetch downstream state.
  onSyncComplete?: (connectorId: string) => void;
  // Shows the real-account (Google) connect card above the demo picker. Defaults
  // to false so the /agents Foundry embed — which relies on the demo-only
  // picker it was built against — renders with zero visual change; only
  // /connectors/page.tsx opts in.
  realAccounts?: boolean;
};

export function ConnectorsPanel({
  workspace = "blueleaf",
  servers,
  onSyncComplete,
  realAccounts = false,
}: ConnectorsPanelProps = {}) {
  const [data, setData] = useState<ConnectorsData | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<Record<string, "registering" | "syncing">>({});
  const [outcomes, setOutcomes] = useState<Record<string, SyncOutcome>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const response = await fetch(
        `/api/connectors?workspace=${encodeURIComponent(workspace)}`,
      );
      const body = await response.json();
      if (!response.ok) throw new Error(describeError(body, `couldn't load connectors (HTTP ${response.status})`));
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
      if (!response.ok) {
        throw new Error(
          describeError(body, `${action === "sync" ? "sync" : "connect"} didn't complete (HTTP ${response.status})`),
        );
      }
      if (action === "sync") {
        const record = body as Record<string, unknown>;
        if ("note" in record) {
          setOutcomes((previous) => ({
            ...previous,
            [connectorId]: {
              kind: "routed",
              note:
                typeof record.note === "string" && record.note
                  ? record.note
                  : "synced into the demo workspace to keep your real corpus clean",
              routedTo: typeof record.routed_to === "string" ? record.routed_to : undefined,
            },
          }));
        } else {
          setOutcomes((previous) => ({ ...previous, [connectorId]: { kind: "report", report: body as SyncReport } }));
        }
      }
      await load(); // Live Map reflects the new register / synced counts
      onSyncComplete?.(connectorId); // let a parent (Foundry) refetch its status
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

  const live = data?.connectors ?? [];
  const connectedIds = new Set(live.map((c) => c.connector_id));
  const syncedAny = Object.values(outcomes).some((outcome) => outcome.kind === "report");

  return (
    <div>
      {realAccounts && (
        <>
          <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-0.013em", marginTop: 4, marginBottom: 10 }}>
            Real accounts
          </div>
          <GoogleConnectCard workspace={workspace} />
        </>
      )}

      {phase === "loading" && (
        <div className="pulse" style={{ marginTop: 28, color: "var(--text-muted)", fontSize: 13.5 }}>
          loading connectors…
        </div>
      )}

      {phase === "error" && (
        <div className="plate rise" style={{ marginTop: 24, padding: "18px 22px", borderColor: "rgba(232,161,60,.35)" }}>
          <div style={{ color: "var(--conf-warn)", fontSize: 13.5, fontWeight: 550 }}>Connectors unavailable</div>
          <div className="receipt" style={{ marginTop: 6 }}>{error}</div>
          <button
            type="button"
            className="chip"
            style={{ marginTop: 10 }}
            onClick={() => {
              setPhase("loading");
              void load();
            }}
          >
            Retry
          </button>
        </div>
      )}

      {phase === "ready" && (
        <>
          {/* Scene A6 — known local (synthetic) servers → Connect → Sync now */}
          <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-0.013em", marginTop: 18, marginBottom: 4 }}>
            Demo sources
            <span className="receipt" style={{ marginLeft: 10, color: "var(--text-faint)" }}>
              {known.length} sandbox server(s) · {connectedIds.size} connected
            </span>
          </div>
          <div style={{ color: "var(--text-muted)", fontSize: 12.5, marginBottom: 10, maxWidth: 560 }}>
            Local sandbox MCP servers with synthetic, PII-free data — safe to explore, never a real
            account, and always kept out of your real corpus.
          </div>

          {known.length === 0 && (
            <div className="plate rise" style={{ padding: "16px 20px", marginBottom: 10 }}>
              <div style={{ fontSize: 13.5, color: "var(--text-muted)" }}>No demo sources available for this view.</div>
            </div>
          )}

          {known.map((id, index) => {
            const connected = connectedIds.has(id);
            const state = busy[id];
            const outcome = outcomes[id];
            const note = notes[id];
            return (
              <div
                key={id}
                className="plate reveal"
                style={{ "--i": Math.min(index, 8), padding: "16px 20px", marginBottom: 10 } as React.CSSProperties}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
                  <span style={{ fontSize: 14.5, fontWeight: 600, letterSpacing: "-0.013em", display: "flex", alignItems: "center", gap: 8 }}>
                    {demoLabel(id)}
                    <span
                      style={{
                        fontSize: 10,
                        fontWeight: 700,
                        letterSpacing: "0.05em",
                        color: "var(--text-faint)",
                        border: "1px solid var(--plate-border)",
                        borderRadius: 6,
                        padding: "2px 6px",
                      }}
                    >
                      DEMO
                    </span>
                  </span>
                  <span className="receipt">
                    {id}
                    {connected ? " · connected" : " · not connected"}
                  </span>
                </div>
                <div style={{ marginTop: 4, color: "var(--text-muted)", fontSize: 12.5 }}>
                  {SERVER_NOTE[id] ?? "local MCP server · synthetic sandbox data"}
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

                  {outcome?.kind === "report" && (
                    <span className="receipt seal">
                      pulled {outcome.report.tools_pulled} tool(s) ·{" "}
                      <span style={{ color: "var(--conf-validated)" }}>{outcome.report.chunks} chunk(s)</span> ·{" "}
                      <span style={{ color: outcome.report.flagged > 0 ? "var(--conf-warn)" : "var(--text-faint)" }}>
                        flagged {outcome.report.flagged}
                      </span>{" "}
                      · {outcome.report.entities} entities · {shortDate(outcome.report.synced_at)}
                    </span>
                  )}
                  {outcome?.kind === "routed" && (
                    <span className="receipt seal" style={{ color: "var(--text-muted)" }}>
                      {outcome.note}
                      {outcome.routedTo ? ` (workspace: ${outcome.routedTo})` : ""}
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
                    {demoLabel(row.connector_id)}
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
        </>
      )}
    </div>
  );
}
