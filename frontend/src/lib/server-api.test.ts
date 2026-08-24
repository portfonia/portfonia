// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { currentAccessToken } = vi.hoisted(() => ({
  currentAccessToken: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ currentAccessToken }));

import { listHoldingsServer } from "./server-api";

const originalFetch = global.fetch;

describe("listHoldingsServer", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("forwards Authorization: Bearer <token> from the session cookie when SSR-rendering", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-ssr");
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify([]), { status: 200 }));
    global.fetch = fetchMock;

    await listHoldingsServer();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("authorization")).toBe("Bearer sb-access-token-ssr");
  });

  it("sends no Authorization header when there is no session", async () => {
    currentAccessToken.mockResolvedValue(null);
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify([]), { status: 200 }));
    global.fetch = fetchMock;

    await listHoldingsServer();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.has("authorization")).toBe(false);
  });

  it("still throws on a non-ok backend response", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-ssr");
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 }));

    await expect(listHoldingsServer()).rejects.toThrow("401");
  });
});
