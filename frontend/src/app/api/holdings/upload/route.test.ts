// @vitest-environment node
//
// This is a Route Handler test — no DOM involved. jsdom (the project
// default, see vitest.config.ts) ships its own File/Blob globals distinct
// from Node's undici-based ones, and NextRequest's internal FormData
// parsing brand-checks against undici's — mixing the two throws an opaque
// webidl assertion. Forcing the real Node environment here sidesteps that
// entirely instead of working around the symptom.
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

const { currentAccessToken } = vi.hoisted(() => ({
  currentAccessToken: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ currentAccessToken }));

import { POST } from "./route";

const originalFetch = global.fetch;

function makeUploadRequest() {
  const form = new FormData();
  form.append("file", new File(["a,b,c"], "holdings.csv", { type: "text/csv" }));
  return new NextRequest("https://portfonia.com/api/holdings/upload", {
    method: "POST",
    body: form,
  });
}

describe("POST /api/holdings/upload proxy route", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("forwards Authorization: Bearer <token> to the backend when a session exists", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-xyz");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "job-1", status: "pending" }), {
        status: 202,
      }),
    );
    global.fetch = fetchMock;

    await POST(makeUploadRequest());

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("authorization")).toBe("Bearer sb-access-token-xyz");
  });

  it("sends no Authorization header when there is no session (backend enforces 401 itself)", async () => {
    currentAccessToken.mockResolvedValue(null);
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ detail: "unauthorized" }), { status: 401 }));
    global.fetch = fetchMock;

    await POST(makeUploadRequest());

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.has("authorization")).toBe(false);
  });

  it("still forwards the multipart body untouched", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-xyz");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "job-1", status: "pending" }), {
        status: 202,
      }),
    );
    global.fetch = fetchMock;

    await POST(makeUploadRequest());

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get("file")).toBeInstanceOf(Blob);
  });
});
