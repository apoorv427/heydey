import { GraphPanel } from "../components/GraphPanel";

export default function Page() {
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Graph</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 13, marginTop: 4, maxWidth: 560 }}>
        The living map of your operation — grown from what you actually ask, never a
        scheduled job. Top 50 entities by activity; click a node for its receipts.
      </p>
      <GraphPanel />
    </div>
  );
}
