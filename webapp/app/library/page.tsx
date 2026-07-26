import { LibraryBar } from "../components/LibraryBar";

export default function Page() {
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Library</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 13, marginTop: 4 }}>
        Semantic search over the corpus — matches meaning, not filenames. Every hit is a
        checkable breadcrumb.
      </p>
      <LibraryBar />
    </div>
  );
}
