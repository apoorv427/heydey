import { NextRequest, NextResponse } from "next/server";
import { proxyJson } from "../../lib/supervisor";

export async function POST(request: NextRequest) {
  const payload = await request.json();
  const { status, body } = await proxyJson("/find", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return NextResponse.json(body, { status });
}
