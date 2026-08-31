// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import { confirmUnsubscribe } from "./actions";

const originalFetch = global.fetch;

function formData(fields: Record<string, string>) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.set(k, v);
  return fd;
}

describe("confirmUnsubscribe action", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("rejects a missing token without calling the backend", async () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    const state = await confirmUnsubscribe(undefined, formData({}));

    expect(state?.error).toBe("invalidOrExpired");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("posts token only, with no altcha field", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ email: "a@b.com" }), { status: 200 }),
      );
    global.fetch = fetchMock;

    await confirmUnsubscribe(undefined, formData({ token: "tok-1" }));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/unsubscribe/confirm");
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body).toEqual({ token: "tok-1" });
  });

  it("returns the unsubscribed email on success", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ email: "a@b.com" }), { status: 200 }),
      );

    const state = await confirmUnsubscribe(undefined, formData({ token: "tok-1" }));

    expect(state?.error).toBeNull();
    expect(state?.email).toBe("a@b.com");
  });

  it("maps a 400 to the invalidOrExpired key, not the raw backend detail", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "invalid or expired unsubscribe link" }), {
        status: 400,
      }),
    );

    const state = await confirmUnsubscribe(undefined, formData({ token: "tok-1" }));

    expect(state?.error).toBe("invalidOrExpired");
    expect(state?.email).toBeUndefined();
  });

  it("maps any other non-2xx status to genericError", async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 500 }));

    const state = await confirmUnsubscribe(undefined, formData({ token: "tok-1" }));

    expect(state?.error).toBe("genericError");
  });
});
