import { FilesView } from "../components/FilesView";

export default function Page() {
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Files</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 13, marginTop: 4, maxWidth: 560 }}>
        What did your AI just make, and where did it come from? Every Heydey-produced file
        carries its run, approval, and receipt. Files found in your folders are shown
        separately — we didn&apos;t make those.
      </p>
      <FilesView />
    </div>
  );
}
