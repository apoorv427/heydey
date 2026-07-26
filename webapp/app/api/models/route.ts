import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

export async function GET() {
  const { status, body } = await proxyJson("/models");
  return NextResponse.json(body, { status });
}

export async function PUT(request: NextRequest) {
  const payload = await request.json();
  const { status, body } = await proxyJson("/models", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  return NextResponse.json(body, { status });
}
