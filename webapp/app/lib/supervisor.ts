// Server-side supervisor client. Reads the per-launch bearer token from
// ~/.heydey/runtime/supervisor.json (0600) — the token NEVER reaches the browser;
// every UI call goes browser -> Next route handler -> supervisor.

import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export const OFFLINE_DETAIL =
  "supervisor offline — start it: api/.venv/bin/python api/heydey_supervisor.py";

export async function supervisorFetch(path: string, init?: RequestInit): Promise<Response> {
  const heydeyHome = process.env.HEYDEY_HOME ?? join(homedir(), ".heydey");
  const raw = await readFile(join(heydeyHome, "runtime", "supervisor.json"), "utf8");
  const runtime = JSON.parse(raw) as { port: number; token: string };
  return fetch(`http://127.0.0.1:${runtime.port}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${runtime.token}`,
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
}

export async function proxyJson(
  path: string,
  init?: RequestInit,
): Promise<{ status: number; body: unknown }> {
  let upstream: Response;
  try {
    upstream = await supervisorFetch(path, init);
  } catch {
    return { status: 503, body: { detail: OFFLINE_DETAIL } };
  }
  try {
    return { status: upstream.status, body: await upstream.json() };
  } catch {
    // reachable but broken (e.g. a 500 with a non-JSON body) is NOT "offline" —
    // conflating the two sent us debugging the wrong layer at S4b
    return {
      status: 502,
      body: { detail: `supervisor answered HTTP ${upstream.status} with a non-JSON body` },
    };
  }
}
