// Server-side proxy for every graph read. The supervisor bearer token is read
// in app/lib/supervisor.ts and NEVER reaches the browser — the client only ever
// talks to this handler.
//
//   /api/graph?workspace=…                 -> GET /graph          (ranked panel)
//   /api/graph?entity=<id>                 -> GET /graph/entity   (node detail)
//   /api/graph?neighbors=<id>&hops=2&limit -> GET /graph/neighbors (2-hop walk)
//   /api/graph?profile=<id|key>&label=…    -> GET /graph/profile  ("everything about X")
//
// The profile mode takes BOTH the numeric id and the display label: the id is
// authoritative, and if the graph has no row under that id (legacy v1 mirror
// ids, for one) we retry under the label instead of handing the user a bare 404.

import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

const ID = /^\d{1,12}$/;

function badId(name: string, value: string) {
  return NextResponse.json(
    {
      detail: `${name} must be a numeric entity id (got ${JSON.stringify(value).slice(0, 40)})`,
      next_step: "click a node on the graph rather than editing the URL",
    },
    { status: 400 },
  );
}

function clamp(raw: string | null, fallback: number, min: number, max: number): number {
  const value = Number(raw);
  if (!Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, Math.trunc(value)));
}

export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const workspace = params.get("workspace") ?? "blueleaf";
  const ws = `workspace=${encodeURIComponent(workspace)}`;

  const neighbors = params.get("neighbors");
  if (neighbors !== null) {
    if (!ID.test(neighbors)) return badId("neighbors", neighbors);
    const hops = clamp(params.get("hops"), 2, 1, 3);
    const limit = clamp(params.get("limit"), 40, 1, 100);
    const { status, body } = await proxyJson(`/graph/neighbors?id=${neighbors}&hops=${hops}&limit=${limit}&${ws}`);
    return NextResponse.json(body, { status });
  }

  const profile = params.get("profile");
  if (profile !== null) {
    if (!ID.test(profile)) return badId("profile", profile);
    const first = await proxyJson(`/graph/profile?key=${profile}&${ws}`);
    if (first.status !== 404) return NextResponse.json(first.body, { status: first.status });
    const label = params.get("label");
    if (!label) return NextResponse.json(first.body, { status: 404 });
    const second = await proxyJson(`/graph/profile?key=${encodeURIComponent(label)}&${ws}`);
    return NextResponse.json(second.body, { status: second.status });
  }

  const entity = params.get("entity");
  if (entity !== null) {
    if (!ID.test(entity)) return badId("entity", entity);
    const { status, body } = await proxyJson(`/graph/entity?id=${entity}&${ws}`);
    return NextResponse.json(body, { status });
  }

  const { status, body } = await proxyJson(`/graph?${ws}`);
  return NextResponse.json(body, { status });
}
