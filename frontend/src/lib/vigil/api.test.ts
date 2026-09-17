import { afterEach, describe, expect, it, vi } from "vitest";

import { getVigilVaultStatus } from "./api";

const originalFetch = global.fetch;

describe("getVigilVaultStatus", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("calls the same-origin /api/vigil/vault rewrite path with no-store caching", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ vault_id: null, phase: "DISARMED", revision: 0 }), { status: 200 }));
    global.fetch = fetchMock;

    await getVigilVaultStatus();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/vigil/vault");
    expect(init.cache).toBe("no-store");
  });

  it("resolves the parsed body on 200, matching the current #504 null-vault shape", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ vault_id: null, phase: "DISARMED", revision: 0 }), { status: 200 }),
    );

    const result = await getVigilVaultStatus();

    expect(result).toEqual({ vault_id: null, phase: "DISARMED", revision: 0 });
  });

  // The nav-visibility check must fail closed on every non-2xx status,
  // including 403 (not the owner) and 503 (feature off) — it must never
  // itself call the shared logout() Server Action the way lib/api.ts's
  // throwOnHttpError does, since a decorative "should the Vigil menu entry
  // show" check has no business forcing a session-wide logout redirect on a
  // plain 401/403.
  it.each([401, 403, 503])("rejects without redirecting on a %d response", async (status) => {
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status }));

    await expect(getVigilVaultStatus()).rejects.toThrow(String(status));
  });
});
