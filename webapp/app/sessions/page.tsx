import { SessionBrowser } from "../components/SessionBrowser";

export default function Page() {
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Sessions</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 13, marginTop: 4, maxWidth: 560 }}>
        Every run the system has made, recallable by what you meant — not when you did it.
        Read-only; deleting a session forgets its receipts too.
      </p>
      <SessionBrowser />
    </div>
  );
}
