import { NextRequest, NextResponse } from "next/server";

import { currentAccessToken } from "@/lib/supabase/server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

// Next.js Turbopack rewrites do not reliably proxy multipart/form-data
// (see `frontend/src/app/api/holdings/upload/route.ts` for the same
// pattern — this route exists for the identical reason). This handler
// also independently derives its own bearer token via `currentAccessToken`
// rather than trusting `proxy.ts`'s header injection, for the same
// filesystem-route-wins-ahead-of-rewrite reason documented there.
//
// Matches `MAX_UPLOAD_BODY_BYTES` in
// `backend/app/services/vigil/objects.py` (10,100,000). Kept as its own
// constant — different language/package, no shared source — and checked
// against the RAW multipart body (headers + boundaries + every field),
// which is strictly larger than the backend's own check (ciphertext +
// inner field bytes only), so this can only reject a request the backend
// would also reject, never the reverse.
const MAX_UPLOAD_BODY_BYTES = 10_100_000;

function errorResponse(status: number, detail: string): NextResponse {
  return NextResponse.json({ detail }, { status });
}

export async function POST(req: NextRequest): Promise<NextResponse> {
  const contentType = req.headers.get("content-type");
  if (!contentType || !contentType.startsWith("multipart/form-data")) {
    return errorResponse(422, "expected multipart/form-data");
  }
  if (!req.body) {
    return errorResponse(422, "empty request body");
  }

  // Bounded streaming read: the cap is enforced chunk-by-chunk as bytes
  // arrive, so an oversized body is rejected mid-stream — never fully
  // buffered into memory first and checked only afterward.
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_UPLOAD_BODY_BYTES) {
        // The cap is already enforced here — the oversized chunk is never
        // pushed to `chunks`, so nothing beyond the limit is buffered.
        // Deliberately not calling `reader.cancel()`: with a genuine
        // incoming HTTP request body it would be a harmless best-effort
        // signal, but a FormData-backed stream's internal encoder can
        // still have an in-flight enqueue scheduled, and cancelling races
        // it into an unhandled rejection outside this function's control.
        // Simply stopping consumption and returning is sufficient; the
        // underlying stream is reclaimed once this response ends the
        // request.
        return errorResponse(413, `upload body exceeds ${MAX_UPLOAD_BODY_BYTES} bytes`);
      }
      chunks.push(value);
    }
  } catch (err) {
    console.error("vigil/objects/upload body read error:", err);
    return errorResponse(400, "failed to read upload body");
  }

  const bounded = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bounded.set(chunk, offset);
    offset += chunk.byteLength;
  }

  let backendForm: FormData;
  try {
    const boundedRequest = new Request(req.url, {
      method: "POST",
      headers: { "content-type": contentType },
      body: bounded,
    });
    backendForm = await boundedRequest.formData();
  } catch (err) {
    console.error("vigil/objects/upload multipart parse error:", err);
    return errorResponse(422, "malformed multipart body");
  }

  const token = await currentAccessToken();
  const headers: HeadersInit = {};
  if (token) headers.authorization = `Bearer ${token}`;
  // #450 Design section 4: "Upload route forwards Origin so backend
  // same-origin checks remain meaningful." The incoming Origin header is
  // browser-set and cannot be spoofed by page JS; if a browser omits it,
  // fall back to the canonical origin Next.js itself resolved for this
  // request rather than trusting any other client-supplied value.
  headers.origin = req.headers.get("origin") ?? req.nextUrl.origin;

  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}/vigil/objects/upload`, {
      method: "POST",
      headers,
      body: backendForm,
    });
  } catch (err) {
    console.error("vigil/objects/upload proxy error:", err);
    return errorResponse(502, "Backend unreachable");
  }

  const body = await res.text();
  return new NextResponse(body, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
