import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const workspace = params.get("workspace") ?? "blueleaf";
  const runId = params.get("run_id");
  const path = runId
    ? `/sessions/detail?run_id=${encodeURIComponent(runId)}&workspace=${encodeURIComponent(workspace)}`
    : `/sessions?workspace=${encodeURIComponent(workspace)}&q=${encodeURIComponent(params.get("q") ?? "")}`;
  const { status, body } = await proxyJson(path);
  return NextResponse.json(body, { status });
}

export async function POST(request: NextRequest) {
  const payload = await request.json(); // {run_id, workspace} — delete (right to forget)
  const { status, body } = await proxyJson("/sessions/delete", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return NextResponse.json(body, { status });
}
