// @vitest-environment node
//
// Same reasoning as src/app/api/holdings/upload/route.test.ts: NextRequest's
// internal FormData parsing brand-checks against Node/undici's File/Blob,
// not jsdom's, so this must run in the `node` environment.
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

const { currentAccessToken } = vi.hoisted(() => ({
  currentAccessToken: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ currentAccessToken }));

import { POST } from "./route";

const originalFetch = global.fetch;

const MANIFEST = JSON.stringify({
  version: 1,
  algorithm: "AES-256-GCM",
  vault_id: "v1",
  object_id: "o1",
  has_password: false,
  file_nonce: "AAAAAAAAAAAAAAAA",
  salt: null,
  kdf: null,
  inner_nonce: null,
});

function makeUploadRequest(
  options: { fileBytes?: Uint8Array; headers?: Record<string, string> } = {},
): NextRequest {
  const form = new FormData();
  form.append("expected_revision", "0");
  form.append("config_id", "c1");
  form.append("object_id", "o1");
  form.append("manifest", MANIFEST);
  form.append("inner", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");
  const fileBytes: Uint8Array<ArrayBuffer> = new Uint8Array(options.fileBytes ?? new Uint8Array([1, 2, 3, 4]));
  form.append("file", new Blob([fileBytes]), "ciphertext.bin");
  return new NextRequest("https://portfonia.com/api/vigil/objects/upload", {
    method: "POST",
    body: form,
    headers: options.headers,
  });
}

describe("POST /api/vigil/objects/upload proxy route", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("forwards Authorization: Bearer <token> when a session exists", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-xyz");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ object_id: "o1", status: "ready", revision: 1 }), { status: 201 }));
    global.fetch = fetchMock;

    await POST(makeUploadRequest());

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("authorization")).toBe("Bearer sb-access-token-xyz");
  });

  it("sends no Authorization header when there is no session (backend enforces 401 itself)", async () => {
    currentAccessToken.mockResolvedValue(null);
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "unauthorized" }), { status: 401 }));
    global.fetch = fetchMock;

    await POST(makeUploadRequest());

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.has("authorization")).toBe(false);
  });

  it("forwards the multipart fields intact with the backend's exact field names", async () => {
    currentAccessToken.mockResolvedValue("token");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ object_id: "o1", status: "ready", revision: 1 }), { status: 201 }));
    global.fetch = fetchMock;

    await POST(makeUploadRequest());

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const form = init.body as FormData;
    expect(form.get("expected_revision")).toBe("0");
    expect(form.get("config_id")).toBe("c1");
    expect(form.get("object_id")).toBe("o1");
    expect(form.get("manifest")).toBe(MANIFEST);
    expect(form.get("inner")).toBe("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");
    expect(form.get("file")).toBeInstanceOf(Blob);
  });

  it("forwards the incoming Origin header when present", async () => {
    currentAccessToken.mockResolvedValue("token");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ object_id: "o1", status: "ready", revision: 1 }), { status: 201 }));
    global.fetch = fetchMock;

    await POST(makeUploadRequest({ headers: { origin: "https://portfonia.com" } }));

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("origin")).toBe("https://portfonia.com");
  });

  it("falls back to the request's own resolved origin when the browser omits Origin", async () => {
    currentAccessToken.mockResolvedValue("token");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ object_id: "o1", status: "ready", revision: 1 }), { status: 201 }));
    global.fetch = fetchMock;

    await POST(makeUploadRequest());

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("origin")).toBe("https://portfonia.com");
  });

  it("rejects a non-multipart content-type with 422 before touching the backend", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const req = new NextRequest("https://portfonia.com/api/vigil/objects/upload", {
      method: "POST",
      body: JSON.stringify({ hello: "world" }),
      headers: { "content-type": "application/json" },
    });

    const res = await POST(req);

    expect(res.status).toBe(422);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an oversized body with 413 before ever calling the backend (bounded streaming read)", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    // Comfortably over the route's 10,100,000-byte cap.
    const oversized = new Uint8Array(10_100_001);
    const res = await POST(makeUploadRequest({ fileBytes: oversized }));

    expect(res.status).toBe(413);
    expect(fetchMock).not.toHaveBeenCalled();
    const body = (await res.json()) as { detail: string };
    expect(body.detail).toMatch(/exceeds/);
  });

  it("proxies the backend's response status and body through unchanged", async () => {
    currentAccessToken.mockResolvedValue("token");
    global.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: { error: "revision_conflict", current_revision: 9 } }), {
          status: 409,
        }),
      );

    const res = await POST(makeUploadRequest());

    expect(res.status).toBe(409);
    const body = (await res.json()) as { detail: { current_revision: number } };
    expect(body.detail.current_revision).toBe(9);
  });

  it("returns 502 when the backend is unreachable", async () => {
    currentAccessToken.mockResolvedValue("token");
    global.fetch = vi.fn().mockRejectedValue(new Error("ECONNREFUSED"));

    const res = await POST(makeUploadRequest());

    expect(res.status).toBe(502);
  });
});
