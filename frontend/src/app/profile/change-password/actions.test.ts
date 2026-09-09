// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { signInWithPassword, updateUser, getUser, getSession } = vi.hoisted(() => ({
  signInWithPassword: vi.fn(),
  updateUser: vi.fn(),
  getUser: vi.fn(),
  getSession: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({
  createClient: async () => ({
    auth: { signInWithPassword, updateUser, getUser, getSession },
  }),
  currentAccessToken: async () => {
    const {
      data: { session },
    } = await getSession();
    return session?.access_token ?? null;
  },
}));

import { changePassword } from "./actions";

const originalFetch = global.fetch;

function formData(fields: Record<string, string>) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.set(k, v);
  return fd;
}

const VALID_FIELDS = {
  currentPassword: "oldpassword",
  newPassword: "newpassword1",
  confirmNewPassword: "newpassword1",
  altcha: "solved-payload",
};

describe("changePassword action", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("rejects missing fields without calling the provider or PoW verify", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await changePassword(
      undefined,
      formData({ currentPassword: "", newPassword: "", confirmNewPassword: "" }),
    );

    expect(state?.error).toBeTruthy();
    expect(state?.success).toBeFalsy();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("rejects a new password under 8 characters without calling the provider", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await changePassword(
      undefined,
      formData({
        currentPassword: "oldpassword",
        newPassword: "short",
        confirmNewPassword: "short",
        altcha: "solved-payload",
      }),
    );

    expect(state?.error).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("rejects mismatched new passwords without calling the provider", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await changePassword(
      undefined,
      formData({
        currentPassword: "oldpassword",
        newPassword: "newpassword1",
        confirmNewPassword: "newpassword2",
        altcha: "solved-payload",
      }),
    );

    expect(state?.error).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(signInWithPassword).not.toHaveBeenCalled();
  });

  it("rejects a missing altcha payload without calling the provider", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await changePassword(
      undefined,
      formData({
        currentPassword: "oldpassword",
        newPassword: "newpassword1",
        confirmNewPassword: "newpassword1",
        altcha: "",
      }),
    );

    expect(state?.error).toBe("Please complete the verification widget before submitting.");
    expect(state?.success).toBeFalsy();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(signInWithPassword).not.toHaveBeenCalled();
    expect(updateUser).not.toHaveBeenCalled();
  });

  it("rejects an invalid PoW solution without calling the provider", async () => {
    getSession.mockResolvedValue({ data: { session: { access_token: "tok" } } });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "invalid captcha" }), { status: 400 }),
    );
    global.fetch = fetchMock;

    const state = await changePassword(undefined, formData(VALID_FIELDS));

    expect(state?.error).toBe("Please complete the verification widget before submitting.");
    expect(state?.success).toBeFalsy();
    expect(fetchMock).toHaveBeenCalled();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/me/change-password/altcha-verify");
    const headers = new Headers(init.headers);
    expect(headers.get("authorization")).toBe("Bearer tok");
    expect(JSON.parse(init.body as string)).toEqual({ altcha: "solved-payload" });
    expect(signInWithPassword).not.toHaveBeenCalled();
    expect(updateUser).not.toHaveBeenCalled();
  });

  it("rejects PoW verify when there is no access token without calling the provider", async () => {
    getSession.mockResolvedValue({ data: { session: null } });
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await changePassword(undefined, formData(VALID_FIELDS));

    expect(state?.error).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(updateUser).not.toHaveBeenCalled();
  });

  it("reports the current password as incorrect without calling updateUser", async () => {
    getSession.mockResolvedValue({ data: { session: { access_token: "tok" } } });
    global.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    signInWithPassword.mockResolvedValue({ error: { message: "invalid_grant" } });

    const state = await changePassword(undefined, formData(VALID_FIELDS));

    expect(state?.error).toBeTruthy();
    expect(state?.success).toBeFalsy();
    expect(updateUser).not.toHaveBeenCalled();
  });

  it("verifies against the session's own email, not a client-submitted one", async () => {
    getSession.mockResolvedValue({ data: { session: { access_token: "tok" } } });
    global.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    getUser.mockResolvedValue({ data: { user: { email: "real@b.com" } } });
    signInWithPassword.mockResolvedValue({ error: null });
    updateUser.mockResolvedValue({ error: null });

    await changePassword(
      undefined,
      formData({
        ...VALID_FIELDS,
        // A forged/stale email field must not steer which account gets verified.
        email: "attacker@evil.com",
      }),
    );

    expect(signInWithPassword).toHaveBeenCalledWith({
      email: "real@b.com",
      password: "oldpassword",
    });
  });

  it("succeeds and calls updateUser once PoW and the current password verify", async () => {
    getSession.mockResolvedValue({ data: { session: { access_token: "tok" } } });
    global.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    signInWithPassword.mockResolvedValue({ error: null });
    updateUser.mockResolvedValue({ error: null });

    const state = await changePassword(undefined, formData(VALID_FIELDS));

    expect(updateUser).toHaveBeenCalledWith({ password: "newpassword1" });
    expect(state?.success).toBe(true);
    expect(state?.error).toBeFalsy();
  });

  it("surfaces a generic error if updateUser itself fails after verification", async () => {
    getSession.mockResolvedValue({ data: { session: { access_token: "tok" } } });
    global.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    signInWithPassword.mockResolvedValue({ error: null });
    updateUser.mockResolvedValue({ error: { message: "boom" } });

    const state = await changePassword(undefined, formData(VALID_FIELDS));

    expect(state?.success).toBeFalsy();
    expect(state?.error).toBeTruthy();
  });
});
