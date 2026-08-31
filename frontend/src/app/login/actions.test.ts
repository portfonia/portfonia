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

import type { Me } from "@/lib/api";
import { login } from "./actions";

function formData(fields: Record<string, string>) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.set(k, v);
  return fd;
}

function me(missing: string[]): Me {
  return {
    email: "a@b.com",
    delivery_email: null,
    email_verified_at: null,
    delivery_email_verified_at: null,
    tos_accepted_at: "2026-08-27T00:00:00Z",
    has_questionnaire: !missing.includes("questionnaire"),
    has_holdings: !missing.includes("holdings"),
    missing,
    pending_email_verifications: [],
  };
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

  it("redirects to /holdings for a new user with onboarding gaps (issue #280 item 3)", async () => {
    signInWithPassword.mockResolvedValue({ error: null });
    getMeServer.mockResolvedValue(me(["holdings"]));

    await login(undefined, formData({ email: "a@b.com", password: "correcthorse" }));

    expect(redirect).toHaveBeenCalledWith("/holdings");
  });

  it("redirects to /profile for a returning user with onboarding complete (issue #280 item 3)", async () => {
    signInWithPassword.mockResolvedValue({ error: null });
    getMeServer.mockResolvedValue(me([]));

    await login(undefined, formData({ email: "a@b.com", password: "correcthorse" }));

    expect(redirect).toHaveBeenCalledWith("/profile");
  });

  it("degrades to /holdings when /me cannot be reached after a successful sign-in", async () => {
    signInWithPassword.mockResolvedValue({ error: null });
    getMeServer.mockRejectedValue(new Error("Backend returned 500"));

    await login(undefined, formData({ email: "a@b.com", password: "correcthorse" }));

    expect(redirect).toHaveBeenCalledWith("/holdings");
  });
});
