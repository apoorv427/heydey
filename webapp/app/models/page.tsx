import { ModelsPanel } from "../components/ModelsPanel";

export default function Page() {
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Models</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 13, marginTop: 4, maxWidth: 560 }}>
        The model that writes an answer is never the model that checks it. Same-family
        pairs are refused at save — this panel cannot be misconfigured into a rubber stamp.
      </p>
      <ModelsPanel />
    </div>
  );
}
