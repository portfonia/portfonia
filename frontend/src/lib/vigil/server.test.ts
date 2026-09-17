// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

const { currentAccessToken } = vi.hoisted(() => ({
  currentAccessToken: vi.fn(),
}));
const { logout } = vi.hoisted(() => ({
  logout: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ currentAccessToken }));
vi.mock("@/lib/auth-actions", () => ({ logout }));

import { getVigilVaultStatusServer } from "./server";

function makeRedirectError(): Error & { digest: string } {
  return Object.assign(new Error("NEXT_REDIRECT"), {
    digest: "NEXT_REDIRECT;replace;/login?reason=expired;307;",
  });
}

const originalFetch = global.fetch;

describe("getVigilVaultStatusServer", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("forwards Authorization: Bearer <token> from the SSR session, same pattern as server-api.ts", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-ssr");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ vault_id: null, phase: "DISARMED", revision: 0 }), { status: 200 }));
    global.fetch = fetchMock;

    await getVigilVaultStatusServer();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("authorization")).toBe("Bearer sb-access-token-ssr");
  });

  it("returns an ok result carrying the parsed vault on 200", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-ssr");
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ vault_id: null, phase: "DISARMED", revision: 0 }), { status: 200 }),
    );

    const result = await getVigilVaultStatusServer();

    expect(result).toEqual({
      status: "ok",
      vault: { vault_id: null, phase: "DISARMED", revision: 0 },
    });
  });

  // 403 (not the configured owner) and 503 (VIGIL_MODE off/misconfigured)
  // are both routine, expected states for a normal user or a not-yet-live
  // deployment — the dashboard renders an "unavailable" card, not an error
  // page, for either.
  it.each([403, 503])("returns an unavailable result on %d, not a thrown error", async (status) => {
    currentAccessToken.mockResolvedValue("sb-access-token-ssr");
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status }));

    const result = await getVigilVaultStatusServer();

    expect(result).toEqual({ status: "unavailable" });
  });

  it("routes a 401 through the shared logout() Server Action, same as every other server-api.ts reader", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-ssr");
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 }));
    const redirectError = makeRedirectError();
    logout.mockRejectedValue(redirectError);

    await expect(getVigilVaultStatusServer()).rejects.toBe(redirectError);
    expect(logout).toHaveBeenCalledWith("expired");
  });

  it("returns an error result on an unexpected backend status without throwing", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-ssr");
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 500 }));

    const result = await getVigilVaultStatusServer();

    expect(result).toEqual({ status: "error" });
  });

  it("returns an error result when the backend is unreachable", async () => {
    currentAccessToken.mockResolvedValue("sb-access-token-ssr");
    global.fetch = vi.fn().mockRejectedValue(new Error("fetch failed"));

    const result = await getVigilVaultStatusServer();

    expect(result).toEqual({ status: "error" });
  });
});
