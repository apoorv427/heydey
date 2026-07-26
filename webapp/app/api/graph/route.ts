import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

export async function GET(request: NextRequest) {
  const workspace = request.nextUrl.searchParams.get("workspace") ?? "blueleaf";
  const entity = request.nextUrl.searchParams.get("entity");
  const path = entity
    ? `/graph/entity?id=${encodeURIComponent(entity)}&workspace=${encodeURIComponent(workspace)}`
    : `/graph?workspace=${encodeURIComponent(workspace)}`;
  const { status, body } = await proxyJson(path);
  return NextResponse.json(body, { status });
}
