// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import { confirmEmailVerification } from "./actions";

const { redirect } = vi.hoisted(() => ({ redirect: vi.fn(() => { throw new Error("NEXT_REDIRECT"); }) }));
vi.mock("next/navigation", () => ({ redirect }));

const originalFetch = global.fetch;

function formData(fields: Record<string, string>) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.set(k, v);
  return fd;
}

describe("confirmEmailVerification action", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("rejects a missing token without calling the backend", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await confirmEmailVerification(undefined, formData({ altcha: "solved" }));

    expect(state?.error).toBe("invalidOrExpired");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a missing altcha payload without calling the backend", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await confirmEmailVerification(undefined, formData({ token: "tok" }));

    expect(state?.error).toBe("genericError");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("posts token and altcha payload to the backend", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ email: "a@b.com" }), { status: 200 }));
    global.fetch = fetchMock;

    await expect(confirmEmailVerification(undefined, formData({ token: "tok-1", altcha: "solved" }))).rejects.toThrow("NEXT_REDIRECT");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/email-verifications/confirm");
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.token).toBe("tok-1");
    expect(body.altcha).toBe("solved");
  });

  it("redirects to /profile on successful confirmation", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ email: "a@b.com" }), { status: 200 }));

    await expect(confirmEmailVerification(undefined, formData({ token: "tok-1", altcha: "solved" }))).rejects.toThrow("NEXT_REDIRECT");
    expect(redirect).toHaveBeenCalledExactlyOnceWith("/profile");
  });

  it("maps a 400 to the invalidOrExpired key, not the raw backend detail", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "invalid or expired verification link" }), {
        status: 400,
      }),
    );

    const state = await confirmEmailVerification(
      undefined,
      formData({ token: "tok-1", altcha: "garbage" }),
    );

    expect(state?.error).toBe("invalidOrExpired");
    expect(state?.email).toBeUndefined();
  });

  it("maps any other non-2xx status to genericError", async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 500 }));

    const state = await confirmEmailVerification(
      undefined,
      formData({ token: "tok-1", altcha: "solved" }),
    );

    expect(state?.error).toBe("genericError");
  });
});
