import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

// Builder B — "what did my AI just make, and where did it come from?"
// Proxies GET /artifacts (Heydey-produced artifacts + their provenance, plus
// query-time OS-folder scan when include_os=true). Server-side token only —
// see app/lib/supervisor.ts.
export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const workspace = params.get("workspace") ?? "blueleaf";
  const limit = params.get("limit") ?? "50";
  const includeOs = params.get("include_os") ?? "true";
  const { status, body } = await proxyJson(
    `/artifacts?workspace=${encodeURIComponent(workspace)}&limit=${encodeURIComponent(limit)}` +
      `&include_os=${encodeURIComponent(includeOs)}`,
  );
  return NextResponse.json(body, { status });
}
