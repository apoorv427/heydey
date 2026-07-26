import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

export async function GET(request: NextRequest) {
  const workspace = request.nextUrl.searchParams.get("workspace") ?? "blueleaf";
  const { status, body } = await proxyJson(`/connectors?workspace=${encodeURIComponent(workspace)}`);
  return NextResponse.json(body, { status });
}

export async function POST(request: NextRequest) {
  const { action, ...payload } = await request.json(); // {action: "register"|"sync", workspace, connector_id}
  const path = action === "sync" ? "/connectors/sync" : "/connectors/register";
  const { status, body } = await proxyJson(path, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return NextResponse.json(body, { status });
}
