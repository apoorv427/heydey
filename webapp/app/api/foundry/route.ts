// F8 — Foundry proxy (§D). GET returns the deterministic INTERVIEW payload +
// scan + specs + last-50 events for the /agents surface; POST routes by action:
// "create" -> POST /workspaces (id-only body); "onboard" -> POST /foundry/onboard
// with {workspace, answers}. The bearer token stays server-side; the browser
// only ever talks to this proxy.

import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

export async function GET(request: NextRequest) {
  const workspace = request.nextUrl.searchParams.get("workspace") ?? "blueleaf";
  const { status, body } = await proxyJson(
    `/foundry/status?workspace=${encodeURIComponent(workspace)}`,
  );
  return NextResponse.json(body, { status });
}

export async function POST(request: NextRequest) {
  const { action, ...payload } = await request.json();
  if (action === "create") {
    // payload: {id}
    const { status, body } = await proxyJson("/workspaces", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    return NextResponse.json(body, { status });
  }
  if (action === "onboard") {
    // payload: {workspace, answers}
    const { status, body } = await proxyJson("/foundry/onboard", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    return NextResponse.json(body, { status });
  }
  return NextResponse.json({ detail: `unknown action ${JSON.stringify(action)}` }, { status: 422 });
}
