// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { bearerAuthHeaders } = vi.hoisted(() => ({
  bearerAuthHeaders: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ bearerAuthHeaders }));

import { getVaultServer } from "./server-api";

const originalFetch = global.fetch;

describe("getVaultServer", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("forwards the bearer on the SSR GET /vault path", async () => {
    bearerAuthHeaders.mockResolvedValue({ authorization: "Bearer tok" });
    const body = {
      phase: "DISARMED",
      revision: 0,
      hold_reason: null,
      held_at: null,
      active_object_id: null,
      active_config_id: null,
      next_check_at: null,
      deadline_at: null,
      heartbeat: {
        last_scan_completed_at: null,
        last_dependency_check_at: null,
        health: "held",
        reason: null,
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));
    global.fetch = fetchMock;

    const loaded = await getVaultServer();
    expect(loaded.status).toBe("ok");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer tok");
  });

  it("maps 401 to unauthenticated without leaking a vault body", async () => {
    bearerAuthHeaders.mockResolvedValue({});
    global.fetch = vi.fn().mockResolvedValue(new Response("nope", { status: 401 }));
    await expect(getVaultServer()).resolves.toEqual({ status: "unauthenticated" });
  });

  it("maps 403 to forbidden and 503 to unavailable", async () => {
    bearerAuthHeaders.mockResolvedValue({ authorization: "Bearer tok" });
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 403 }));
    await expect(getVaultServer()).resolves.toEqual({ status: "forbidden" });
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 503 }));
    await expect(getVaultServer()).resolves.toEqual({ status: "unavailable" });
  });
});
