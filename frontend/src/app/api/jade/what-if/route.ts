import { NextRequest, NextResponse } from "next/server";

import { currentAccessToken } from "@/lib/supabase/server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

const WHAT_IF_TIMEOUT_MS = 90_000;

export async function POST(req: NextRequest): Promise<NextResponse> {
  const body = await req.text();
  const token = await currentAccessToken();
  const headers: HeadersInit = { "content-type": "application/json" };
  if (token) headers.authorization = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}/jade/what-if`, {
      method: "POST",
      headers,
      body,
      signal: AbortSignal.timeout(WHAT_IF_TIMEOUT_MS),
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof Error && err.name === "TimeoutError") {
      return NextResponse.json({ detail: "Backend timeout" }, { status: 504 });
    }
    console.error("jade/what-if proxy error:", err);
    return NextResponse.json({ detail: "Backend unreachable" }, { status: 502 });
  }

  const responseHeaders: HeadersInit = { "Content-Type": "application/json" };
  const retryAfter = res.headers.get("Retry-After");
  if (retryAfter !== null) responseHeaders["Retry-After"] = retryAfter;

  return new NextResponse(await res.text(), {
    status: res.status,
    headers: responseHeaders,
  });
}
