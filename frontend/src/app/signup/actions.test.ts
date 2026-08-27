// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { signInWithPassword, redirect } = vi.hoisted(() => ({
  signInWithPassword: vi.fn(),
  redirect: vi.fn(),
}));

vi.mock("next/navigation", () => ({ redirect }));
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
      formData({ invite_token: "tok", email: "a@b.com", password: "correcthorse" }),
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
      formData({ invite_token: "tok", email: "a@b.com", password: "correcthorse" }),
    );

    expect(state?.error).toBe("Temporarily unavailable. Try again later.");
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("surfaces the backend's rejection reason (expired/used/revoked invite) verbatim", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "This invite is no longer valid." }), {
        status: 400,
      }),
    );

    const state = await signup(
      undefined,
      formData({ invite_token: "tok", email: "a@b.com", password: "correcthorse" }),
    );

    expect(state?.error).toBe("This invite is no longer valid.");
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("on backend success, signs in with the same credentials and redirects to /holdings (single-step signup)", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "u1", email: "a@b.com" }), { status: 201 }),
    );
    signInWithPassword.mockResolvedValue({ error: null });

    await signup(
      undefined,
      formData({ invite_token: "tok", email: "a@b.com", password: "correcthorse" }),
    );

    expect(signInWithPassword).toHaveBeenCalledWith({
      email: "a@b.com",
      password: "correcthorse",
    });
    expect(redirect).toHaveBeenCalledWith("/holdings");
  });

  it("if the account was created but auto-login fails, redirects to /login rather than erroring", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "u1", email: "a@b.com" }), { status: 201 }),
    );
    signInWithPassword.mockResolvedValue({ error: { message: "boom" } });

    await signup(
      undefined,
      formData({ invite_token: "tok", email: "a@b.com", password: "correcthorse" }),
    );

    expect(redirect).toHaveBeenCalledWith("/login");
  });
});
