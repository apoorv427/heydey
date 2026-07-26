import { TodayView } from "../components/TodayView";

export default function Page() {
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Today</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 13, marginTop: 4 }}>
        The overnight brief with receipts, and every action waiting for your tap.
      </p>
      <TodayView />
    </div>
  );
}
