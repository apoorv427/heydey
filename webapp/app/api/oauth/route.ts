import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

// Proxies the real-account OAuth connect flow (Builder-D contract):
//   GET  /connectors/oauth/status?connector_id&workspace
//   POST /connectors/oauth/config      {connector_id, workspace, client_id, client_secret?}
//   POST /connectors/oauth/start       {connector_id, workspace}
//   POST /connectors/oauth/disconnect  {connector_id, workspace}
// Same pattern as app/api/connectors/route.ts — the bearer token stays
// server-side in supervisor.ts; the browser only ever talks to this route.

export async function GET(request: NextRequest) {
  const workspace = request.nextUrl.searchParams.get("workspace") ?? "blueleaf";
  const connectorId = request.nextUrl.searchParams.get("connector_id");
  if (!connectorId) {
    return NextResponse.json({ detail: "connector_id is required" }, { status: 400 });
  }
  const { status, body } = await proxyJson(
    `/connectors/oauth/status?connector_id=${encodeURIComponent(connectorId)}&workspace=${encodeURIComponent(workspace)}`,
  );
  return NextResponse.json(body, { status });
}

const ACTION_PATH: Record<string, string> = {
  config: "/connectors/oauth/config",
  start: "/connectors/oauth/start",
  disconnect: "/connectors/oauth/disconnect",
};

export async function POST(request: NextRequest) {
  const { action, ...payload } = await request.json();
  const path = ACTION_PATH[action];
  if (!path) {
    return NextResponse.json({ detail: `unknown oauth action ${action ?? "(none)"}` }, { status: 400 });
  }
  const { status, body } = await proxyJson(path, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return NextResponse.json(body, { status });
}
