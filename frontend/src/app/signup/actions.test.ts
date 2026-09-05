// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { signInWithPassword, redirect, headerStore } = vi.hoisted(() => ({
  signInWithPassword: vi.fn(),
  redirect: vi.fn(),
  headerStore: new Map<string, string>(),
}));

vi.mock("next/navigation", () => ({ redirect }));
vi.mock("next/headers", () => ({
  headers: async () => ({
    get: (name: string) => headerStore.get(name.toLowerCase()) ?? null,
  }),
}));
vi.mock("@/lib/supabase/server", () => ({
  createClient: async () => ({ auth: { signInWithPassword } }),
}));

import { signup } from "./actions";

const originalFetch = global.fetch;

function formData(fields: Record<string, string>) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.set(k, v);
  return fd;
}

describe("signup action", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    headerStore.clear();
    vi.resetAllMocks();
  });

  it("rejects a missing invite token without calling the backend", async () => {
    const state = await signup(
      undefined,
      formData({ invite_token: "", email: "a@b.com", password: "correcthorse" }),
    );

    expect(state?.error).toBeTruthy();
  });

  it("rejects a short password without calling the backend", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await signup(
      undefined,
      formData({ invite_token: "tok", email: "a@b.com", password: "short" }),
    );

    expect(state?.error).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows a retryable message on 429 instead of the generic failure path", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "too many attempts, try again later" }), {
        status: 429,
        headers: { "Retry-After": "60" },
      }),
    );

    const state = await signup(
      undefined,
      formData({
      invite_token: "tok",
      email: "a@b.com",
      password: "correcthorse",
      tos_accepted: "on",
    }),
    );

    expect(state?.error).toBe("too many attempts, try again later");
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("shows a retryable message on 503 even if detail is not a string", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: { code: "down" } }), { status: 503 }),
    );

    const state = await signup(
      undefined,
      formData({
      invite_token: "tok",
      email: "a@b.com",
      password: "correcthorse",
      tos_accepted: "on",
    }),
    );

    expect(state?.error).toBe("Temporarily unavailable. Try again later.");
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("forwards Caddy's X-Forwarded-For and X-Real-IP on the backend signup fetch", async () => {
    headerStore.set("x-forwarded-for", "203.0.113.50, 10.0.0.2");
    headerStore.set("x-real-ip", "203.0.113.50");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "u1", email: "a@b.com" }), { status: 201 }),
    );
    global.fetch = fetchMock;
    signInWithPassword.mockResolvedValue({ error: null });

    await signup(
      undefined,
      formData({
      invite_token: "tok",
      email: "a@b.com",
      password: "correcthorse",
      tos_accepted: "on",
    }),
    );

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("x-forwarded-for")).toBe("203.0.113.50, 10.0.0.2");
    expect(headers.get("x-real-ip")).toBe("203.0.113.50");
  });

  it("omits forwarded-client headers when Caddy did not set them", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "u1", email: "a@b.com" }), { status: 201 }),
    );
    global.fetch = fetchMock;
    signInWithPassword.mockResolvedValue({ error: null });

    await signup(
      undefined,
      formData({
      invite_token: "tok",
      email: "a@b.com",
      password: "correcthorse",
      tos_accepted: "on",
    }),
    );

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.has("x-forwarded-for")).toBe(false);
    expect(headers.has("x-real-ip")).toBe(false);
  });

  it("surfaces the backend's rejection reason (expired/used/revoked invite) verbatim", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "This invite is no longer valid." }), {
        status: 400,
      }),
    );

    const state = await signup(
      undefined,
      formData({
      invite_token: "tok",
      email: "a@b.com",
      password: "correcthorse",
      tos_accepted: "on",
    }),
    );

    expect(state?.error).toBe("This invite is no longer valid.");
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("on backend success, signs in with the same credentials and redirects to /questionnaire?onboarding=1 (issue #221)", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "u1", email: "a@b.com" }), { status: 201 }),
    );
    signInWithPassword.mockResolvedValue({ error: null });

    await signup(
      undefined,
      formData({
      invite_token: "tok",
      email: "a@b.com",
      password: "correcthorse",
      tos_accepted: "on",
    }),
    );

    expect(signInWithPassword).toHaveBeenCalledWith({
      email: "a@b.com",
      password: "correcthorse",
    });
    expect(redirect).toHaveBeenCalledWith("/questionnaire?onboarding=1");
  });

  it("forwards tos_accepted to the backend as a JSON boolean, checked from the checkbox value", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "u1", email: "a@b.com" }), { status: 201 }),
    );
    global.fetch = fetchMock;
    signInWithPassword.mockResolvedValue({ error: null });

    await signup(
      undefined,
      formData({
        invite_token: "tok",
        email: "a@b.com",
        password: "correcthorse",
        tos_accepted: "on",
      }),
    );

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.tos_accepted).toBe(true);
  });

  // zh-Hant reaches this mapping through a real form submission since issue
  // #350 item 4 lifted its UNREVIEWED_LOCALES gate — resolveLocale's
  // isLocale() no longer falls it back to DEFAULT_LOCALE.
  it.each([
    ["en", "en"],
    ["zh-Hans", "zh"],
    ["zh-Hant", "zh"],
  ])(
    "maps the UI locale %s to the bare backend code %s and forwards it as `locale` (issue #308)",
    async (uiLocale, backendCode) => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ id: "u1", email: "a@b.com" }), { status: 201 }),
      );
      global.fetch = fetchMock;
      signInWithPassword.mockResolvedValue({ error: null });

      await signup(
        undefined,
        formData({
          invite_token: "tok",
          email: "a@b.com",
          password: "correcthorse",
          tos_accepted: "on",
          locale: uiLocale,
        }),
      );

      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      expect(body.locale).toBe(backendCode);
    },
  );

  it("falls back to the default UI locale's backend code when no `locale` form field is present", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "u1", email: "a@b.com" }), { status: 201 }),
    );
    global.fetch = fetchMock;
    signInWithPassword.mockResolvedValue({ error: null });

    await signup(
      undefined,
      formData({
        invite_token: "tok",
        email: "a@b.com",
        password: "correcthorse",
        tos_accepted: "on",
      }),
    );

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.locale).toBe("en");
  });

  it("if the account was created but auto-login fails, redirects to /login rather than erroring", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "u1", email: "a@b.com" }), { status: 201 }),
    );
    signInWithPassword.mockResolvedValue({ error: { message: "boom" } });

    await signup(
      undefined,
      formData({
      invite_token: "tok",
      email: "a@b.com",
      password: "correcthorse",
      tos_accepted: "on",
    }),
    );

    expect(redirect).toHaveBeenCalledWith("/login");
  });
});
