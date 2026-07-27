import { ConnectorsPanel } from "../components/ConnectorsPanel";

export default function Page() {
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Connectors</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 13, marginTop: 4, maxWidth: 560 }}>
        Connect a real Google account, or explore with synthetic demo sources first — every
        pull runs through the injection guard before a single chunk is stored. Connect a
        source, sync it, and watch the clean rows (and any flagged ones) appear on the Live Map.
      </p>
      <ConnectorsPanel realAccounts />
    </div>
  );
}
