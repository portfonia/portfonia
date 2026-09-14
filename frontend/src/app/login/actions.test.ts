// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { signInWithPassword, redirect, getMeServer } = vi.hoisted(() => ({
  signInWithPassword: vi.fn(),
  redirect: vi.fn(),
  getMeServer: vi.fn(),
}));

vi.mock("next/navigation", () => ({ redirect }));
vi.mock("@/lib/supabase/server", () => ({
  createClient: async () => ({ auth: { signInWithPassword } }),
}));
vi.mock("@/lib/server-api", () => ({ getMeServer }));

import { login } from "./actions";

function formData(fields: Record<string, string>) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.set(k, v);
  return fd;
}

describe("login action", () => {
  afterEach(() => vi.resetAllMocks());

  it("rejects a missing email or password without calling the provider", async () => {
    const state = await login(undefined, formData({ email: "", password: "" }));

    expect(state?.error).toBeTruthy();
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("returns a generic error on invalid credentials, not the provider's own message", async () => {
    signInWithPassword.mockResolvedValue({ error: { message: "invalid_grant: bad password" } });

    const state = await login(
      undefined,
      formData({ email: "a@b.com", password: "wrongpassword" }),
    );

    expect(state?.error).toBeTruthy();
    expect(state?.error).not.toContain("invalid_grant");
    expect(redirect).not.toHaveBeenCalled();
  });

  it("redirects to /profile on success without a /me round-trip (issue #280 item 3)", async () => {
    signInWithPassword.mockResolvedValue({ error: null });

    await login(undefined, formData({ email: "a@b.com", password: "correcthorse" }));

    expect(redirect).toHaveBeenCalledWith("/profile");
    expect(getMeServer).not.toHaveBeenCalled();
  });

  it("redirects to /auth/vigil when next is exactly that path", async () => {
    signInWithPassword.mockResolvedValue({ error: null });

    await login(
      undefined,
      formData({ email: "a@b.com", password: "correcthorse", next: "/auth/vigil" }),
    );

    expect(redirect).toHaveBeenCalledWith("/auth/vigil");
  });

  it.each([
    "",
    "/profile",
    "/auth/vigil/extra",
    "/auth/vigil?x=1",
    "/login?next=/auth/vigil",
    "https://portfonia.com/auth/vigil",
    "//evil.example/auth/vigil",
    "/profile/auth/vigil",
    "/AUTH/VIGIL",
    " /auth/vigil",
    "/auth/vigil ",
    "%2Fauth%2Fvigil",
  ])("ignores next=%j and lands on /profile", async (next) => {
    signInWithPassword.mockResolvedValue({ error: null });

    await login(undefined, formData({ email: "a@b.com", password: "correcthorse", next }));

    expect(redirect).toHaveBeenCalledWith("/profile");
  });
});
