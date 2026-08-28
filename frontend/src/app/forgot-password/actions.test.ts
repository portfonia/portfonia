// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { headerStore } = vi.hoisted(() => ({
  headerStore: new Map<string, string>(),
}));

vi.mock("next/headers", () => ({
  headers: async () => ({
    get: (name: string) => headerStore.get(name.toLowerCase()) ?? null,
  }),
}));

import { forgotPassword } from "./actions";

const originalFetch = global.fetch;

function formData(fields: Record<string, string>) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.set(k, v);
  return fd;
}

describe("forgotPassword action", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    headerStore.clear();
    vi.resetAllMocks();
  });

  it("rejects a missing email without calling the backend", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await forgotPassword(undefined, formData({ email: "", altcha: "solved" }));

    expect(state?.error).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a missing altcha payload without calling the backend", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await forgotPassword(undefined, formData({ email: "a@b.com", altcha: "" }));

    expect(state?.error).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("posts email and altcha payload to the backend, lowercasing/trimming the email", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ account_found: true }), { status: 200 }),
    );
    global.fetch = fetchMock;

    await forgotPassword(
      undefined,
      formData({ email: "  A@B.com", altcha: "solved-payload" }),
    );

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/auth/forgot-password");
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    // NB: the action trims but does not lowercase — the backend does that
    // (auth.py: `req.email.strip().lower()`), matching signup's split of
    // responsibility.
    expect(body.email).toBe("A@B.com");
    expect(body.altcha).toBe("solved-payload");
  });

  it("returns accountFound=true from a 200 response", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ account_found: true }), { status: 200 }),
    );

    const state = await forgotPassword(
      undefined,
      formData({ email: "a@b.com", altcha: "solved" }),
    );

    expect(state?.error).toBeNull();
    expect(state?.accountFound).toBe(true);
  });

  it("returns accountFound=false from a 200 response", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ account_found: false }), { status: 200 }),
    );

    const state = await forgotPassword(
      undefined,
      formData({ email: "nobody@b.com", altcha: "solved" }),
    );

    expect(state?.accountFound).toBe(false);
  });

  it("surfaces a captcha error on a 400 (invalid PoW solution)", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "invalid captcha" }), { status: 400 }),
    );

    const state = await forgotPassword(
      undefined,
      formData({ email: "a@b.com", altcha: "garbage" }),
    );

    expect(state?.error).toBe("invalid captcha");
    expect(state?.accountFound).toBeUndefined();
  });

  it("shows a retryable message on 429", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "too many attempts, try again later" }), {
        status: 429,
      }),
    );

    const state = await forgotPassword(
      undefined,
      formData({ email: "a@b.com", altcha: "solved" }),
    );

    expect(state?.error).toBe("too many attempts, try again later");
  });

  it("shows a retryable message on 503 even if detail is not a string", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: { code: "down" } }), { status: 503 }),
    );

    const state = await forgotPassword(
      undefined,
      formData({ email: "a@b.com", altcha: "solved" }),
    );

    expect(state?.error).toBe("Temporarily unavailable. Try again later.");
  });

  it("forwards Caddy's X-Forwarded-For and X-Real-IP on the backend fetch (issue #190/#231)", async () => {
    headerStore.set("x-forwarded-for", "203.0.113.50, 10.0.0.2");
    headerStore.set("x-real-ip", "203.0.113.50");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ account_found: false }), { status: 200 }),
    );
    global.fetch = fetchMock;

    await forgotPassword(undefined, formData({ email: "a@b.com", altcha: "solved" }));

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("x-forwarded-for")).toBe("203.0.113.50, 10.0.0.2");
    expect(headers.get("x-real-ip")).toBe("203.0.113.50");
  });
});
