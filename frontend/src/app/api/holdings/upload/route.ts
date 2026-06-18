import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

// Next.js Turbopack rewrites do not reliably proxy multipart/form-data.
// This API route manually forwards the upload to the FastAPI backend.
export async function POST(req: NextRequest): Promise<NextResponse> {
  const formData = await req.formData();
  const backendForm = new FormData();
  for (const [key, value] of formData.entries()) {
    backendForm.append(key, value);
  }

  let res: Response;
  try {
    res = await fetch(`${BACKEND_URL}/holdings/upload`, {
      method: "POST",
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
