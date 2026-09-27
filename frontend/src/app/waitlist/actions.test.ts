// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { headerStore } = vi.hoisted(() => ({ headerStore: new Map<string, string>() }));
vi.mock("next/headers", () => ({
  headers: async () => ({ get: (name: string) => headerStore.get(name.toLowerCase()) ?? null }),
}));

import { submitWaitlist } from "./actions";

const originalFetch = global.fetch;

function formData(fields: Record<string, string>) {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.set(key, value);
  return data;
}

describe("submitWaitlist action", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    headerStore.clear();
    vi.resetAllMocks();
  });

  it("forwards email, selected locale, proof, and client IP headers", async () => {
    headerStore.set("x-forwarded-for", "203.0.113.9");
    headerStore.set("x-real-ip", "203.0.113.9");
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"received":true}', { status: 200 }));
    global.fetch = fetchMock;
    const state = await submitWaitlist(
      undefined,
      formData({ email: "  A@X.COM  ", locale: "zh-Hant", altcha: "proof" }),
    );
    expect(state).toEqual({ error: null, received: true });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/waitlist");
    expect(JSON.parse(init.body as string)).toEqual({ email: "A@X.COM", locale: "zh-Hant", altcha: "proof" });
    expect(init.headers).toMatchObject({ "X-Forwarded-For": "203.0.113.9", "X-Real-IP": "203.0.113.9" });
  });

  it.each([400, 429, 503, 500])("maps HTTP %i to catalog copy", async (status) => {
    global.fetch = vi.fn().mockResolvedValue(new Response(null, { status }));
    const state = await submitWaitlist(
      undefined,
      formData({ email: "a@x.com", locale: "en", altcha: "proof" }),
    );
    expect(state.received).toBeUndefined();
    expect(state.error).toBeTruthy();
  });

  it("requires email and captcha before the request", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;
    expect((await submitWaitlist(undefined, formData({ email: "", altcha: "proof" }))).error).toBeTruthy();
    expect((await submitWaitlist(undefined, formData({ email: "a@x.com", altcha: "" }))).error).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
