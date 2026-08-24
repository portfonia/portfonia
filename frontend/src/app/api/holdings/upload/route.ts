import { NextRequest, NextResponse } from "next/server";

import { currentAccessToken } from "@/lib/supabase/server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

// Next.js Turbopack rewrites do not reliably proxy multipart/form-data.
// This API route manually forwards the upload to the FastAPI backend.
//
// This is the one path proxy.ts's own Authorization-header injection does
// NOT cover (Ring 1-B design doc §7.3(1)): proxy treats every /api/*
// request as a rewrite target, but this file's own route.ts wins the
// filesystem-route match ahead of that rewrite (Next's execution order),
// so it never sees proxy's injected header on its OWN outbound fetch below
// — it must derive the token itself. No session simply means no header;
// the backend enforces 401 on its own (defense in depth, not double work).
export async function POST(req: NextRequest): Promise<NextResponse> {
  const formData = await req.formData();
  const backendForm = new FormData();
  for (const [key, value] of formData.entries()) {
    backendForm.append(key, value);
  }

  const token = await currentAccessToken();
  const headers: HeadersInit = {};
  if (token) headers.authorization = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}/holdings/upload`, {
      method: "POST",
      headers,
      body: backendForm,
    });
  } catch (err) {
    console.error("holdings/upload proxy error:", err);
    return NextResponse.json(
      { detail: "Backend unreachable" },
      { status: 502 },
    );
  }

  const body = await res.text();
  return new NextResponse(body, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
