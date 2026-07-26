import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

export async function GET(request: NextRequest) {
  const workspace = request.nextUrl.searchParams.get("workspace") ?? "blueleaf";
  const { status, body } = await proxyJson(`/today?workspace=${encodeURIComponent(workspace)}`);
  return NextResponse.json(body, { status });
}
