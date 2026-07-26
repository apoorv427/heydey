// Status — supervisor health, moved from the S0 root page when Ask took "/".

import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export const dynamic = "force-dynamic";

type Health = {
  status: string;
  service: string;
  version: string;
  schema_version: number;
  workspaces: number;
  jobs: { owner: string; active: number };
};

type SupervisorState =
  | { ok: true; port: number; health: Health }
  | { ok: false; detail: string };

async function getSupervisorState(): Promise<SupervisorState> {
  try {
    const heydeyHome = process.env.HEYDEY_HOME ?? join(homedir(), ".heydey");
    const raw = await readFile(join(heydeyHome, "runtime", "supervisor.json"), "utf8");
    const runtime = JSON.parse(raw) as { port: number; token: string };
    const response = await fetch(`http://127.0.0.1:${runtime.port}/health`, {
      headers: { Authorization: `Bearer ${runtime.token}` },
      cache: "no-store",
    });
    if (!response.ok) {
      return { ok: false, detail: `supervisor answered HTTP ${response.status}` };
    }
    return { ok: true, port: runtime.port, health: (await response.json()) as Health };
  } catch {
    return {
      ok: false,
      detail: "supervisor not reachable — start it: api/.venv/bin/python api/heydey_supervisor.py",
    };
  }
}

export default async function Page() {
  const state = await getSupervisorState();
  return (
    <div className="rise">
      <h1 style={{ fontSize: 22, fontWeight: 600 }}>Status</h1>
      <div className="plate" style={{ marginTop: 24, padding: "20px 24px", fontSize: 14, lineHeight: 1.7 }}>
        {state.ok ? (
          <>
            <div style={{ color: "var(--conf-validated)" }}>
              ● supervisor ok on 127.0.0.1:{state.port}
            </div>
            <div className="receipt">
              {state.health.service} v{state.health.version} · schema v{state.health.schema_version} ·{" "}
              {state.health.workspaces} workspace(s) · jobs owner: {state.health.jobs.owner}
            </div>
          </>
        ) : (
          <>
            <div style={{ color: "var(--conf-warn)" }}>● supervisor offline</div>
            <div className="receipt">{state.detail}</div>
          </>
        )}
      </div>
    </div>
  );
}
