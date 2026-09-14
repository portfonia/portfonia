// @vitest-environment node
import { describe, expect, it, vi } from "vitest";

const { signOut, redirect } = vi.hoisted(() => ({
  signOut: vi.fn(),
  redirect: vi.fn(),
}));

vi.mock("next/navigation", () => ({ redirect }));
vi.mock("@/lib/supabase/server", () => ({
  createClient: async () => ({ auth: { signOut } }),
}));

import { logout } from "./actions";

describe("logout", () => {
  it("signs out the shared Auth session and returns to the shell", async () => {
    signOut.mockResolvedValue({ error: null });
    await logout();
    expect(signOut).toHaveBeenCalled();
    expect(redirect).toHaveBeenCalledWith("/");
  });
});
