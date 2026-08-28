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

import { listHoldingsServer } from "./server-api";

// redirect() always throws internally (a NEXT_REDIRECT digest the Next.js
// runtime intercepts) — logout() never resolves normally in production, so
// the mock mirrors that instead of resolving.
function makeRedirectError(): Error & { digest: string } {
  return Object.assign(new Error("NEXT_REDIRECT"), {
    digest: "NEXT_REDIRECT;replace;/login?reason=expired;307;",
  });
}

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
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 500 }));

    await expect(listHoldingsServer()).rejects.toThrow("500");
  });

  it("routes a 401 through the shared logout() Server Action (issue #235/#240)", async () => {
    // A 401 here can be the server-side idle timeout firing on a reopened,
    // previously-idle tab — before any client-side idle timer has run.
    // This must drive the same logout()+redirect() the client timer uses,
    // not just throw a generic error the page render never recovers from.
    currentAccessToken.mockResolvedValue("sb-access-token-ssr");
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 }));
    const redirectError = makeRedirectError();
    logout.mockRejectedValue(redirectError);

    await expect(listHoldingsServer()).rejects.toBe(redirectError);
    expect(logout).toHaveBeenCalledWith("expired");
  });
});
