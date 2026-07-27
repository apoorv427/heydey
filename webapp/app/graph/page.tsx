import { GraphPanel } from "../components/GraphPanel";

export default function Page() {
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Graph</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 13, marginTop: 4, maxWidth: 640 }}>
        The living map of your operation — grown from what you actually ingest and ask, never a
        scheduled crawl. Drag a node to pin it, scroll to zoom, click one to pull in its audited
        2-hop neighbourhood and open everything the graph knows about it.
      </p>
      <GraphPanel />
    </div>
  );
}
