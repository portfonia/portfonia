// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { signInWithPassword, updateUser, getUser } = vi.hoisted(() => ({
  signInWithPassword: vi.fn(),
  updateUser: vi.fn(),
  getUser: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({
  createClient: async () => ({
    auth: { signInWithPassword, updateUser, getUser },
  }),
}));

import { changePassword } from "./actions";

function formData(fields: Record<string, string>) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.set(k, v);
  return fd;
}

describe("changePassword action", () => {
  afterEach(() => vi.resetAllMocks());

  it("rejects missing fields without calling the provider", async () => {
    const state = await changePassword(
      undefined,
      formData({ currentPassword: "", newPassword: "", confirmNewPassword: "" }),
    );

    expect(state?.error).toBeTruthy();
    expect(state?.success).toBeFalsy();
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("rejects a new password under 8 characters without calling the provider", async () => {
    const state = await changePassword(
      undefined,
      formData({
        currentPassword: "oldpassword",
        newPassword: "short",
        confirmNewPassword: "short",
      }),
    );

    expect(state?.error).toBeTruthy();
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("rejects mismatched new passwords without calling the provider", async () => {
    const state = await changePassword(
      undefined,
      formData({
        currentPassword: "oldpassword",
        newPassword: "newpassword1",
        confirmNewPassword: "newpassword2",
      }),
    );

    expect(state?.error).toBeTruthy();
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("reports the current password as incorrect without calling updateUser", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    signInWithPassword.mockResolvedValue({ error: { message: "invalid_grant" } });

    const state = await changePassword(
      undefined,
      formData({
        currentPassword: "wrongpassword",
        newPassword: "newpassword1",
        confirmNewPassword: "newpassword1",
      }),
    );

    expect(state?.error).toBeTruthy();
    expect(state?.success).toBeFalsy();
    expect(updateUser).not.toHaveBeenCalled();
  });

  it("verifies against the session's own email, not a client-submitted one", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "real@b.com" } } });
    signInWithPassword.mockResolvedValue({ error: null });
    updateUser.mockResolvedValue({ error: null });

    await changePassword(
      undefined,
      formData({
        currentPassword: "oldpassword",
        newPassword: "newpassword1",
        confirmNewPassword: "newpassword1",
        // A forged/stale email field must not steer which account gets verified.
        email: "attacker@evil.com",
      }),
    );

    expect(signInWithPassword).toHaveBeenCalledWith({
      email: "real@b.com",
      password: "oldpassword",
    });
  });

  it("succeeds and calls updateUser once the current password verifies", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    signInWithPassword.mockResolvedValue({ error: null });
    updateUser.mockResolvedValue({ error: null });

    const state = await changePassword(
      undefined,
      formData({
        currentPassword: "oldpassword",
        newPassword: "newpassword1",
        confirmNewPassword: "newpassword1",
      }),
    );

    expect(updateUser).toHaveBeenCalledWith({ password: "newpassword1" });
    expect(state?.success).toBe(true);
    expect(state?.error).toBeFalsy();
  });

  it("surfaces a generic error if updateUser itself fails after verification", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    signInWithPassword.mockResolvedValue({ error: null });
    updateUser.mockResolvedValue({ error: { message: "boom" } });

    const state = await changePassword(
      undefined,
      formData({
        currentPassword: "oldpassword",
        newPassword: "newpassword1",
        confirmNewPassword: "newpassword1",
      }),
    );

    expect(state?.success).toBeFalsy();
    expect(state?.error).toBeTruthy();
  });
});
