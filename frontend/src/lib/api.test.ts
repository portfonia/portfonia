import { afterEach, describe, expect, it, vi } from "vitest";

const { logout } = vi.hoisted(() => ({
  logout: vi.fn(),
}));

vi.mock("@/lib/auth-actions", () => ({ logout }));

import { ApiError, exportHoldings, listHoldings } from "./api";

const originalFetch = global.fetch;

// redirect() always throws internally (a NEXT_REDIRECT digest the Next.js
// runtime intercepts) — logout() never resolves normally in production, so
// the mock mirrors that instead of resolving.
function makeRedirectError(): Error & { digest: string } {
  return Object.assign(new Error("NEXT_REDIRECT"), {
    digest: "NEXT_REDIRECT;replace;/login?reason=expired;307;",
  });
}

describe("listHoldings", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("still throws ApiError on a non-401 non-ok response", async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 500 }));

    await expect(listHoldings()).rejects.toThrow(ApiError);
    expect(logout).not.toHaveBeenCalled();
  });

  it("routes a 401 through the shared logout() Server Action (issue #235/#240)", async () => {
    // A 401 here can be the server-side idle timeout firing on a reopened,
    // previously-idle tab — before any client-side idle timer has run.
    // This must drive the same logout()+redirect() the client timer uses,
    // not leave the caller to throw a generic ApiError nothing recovers from.
    global.fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 }));
    const redirectError = makeRedirectError();
    logout.mockRejectedValue(redirectError);

    await expect(listHoldings()).rejects.toBe(redirectError);
    expect(logout).toHaveBeenCalledWith("expired");
  });
});

describe("exportHoldings", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("uses the Content-Disposition filename from GET /holdings/export", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response("##### export\n", {
        status: 200,
        headers: {
          "Content-Type": "text/markdown",
          "Content-Disposition": 'attachment; filename="holdings-20260902-051530Z.md"',
        },
      }),
    );
    const result = await exportHoldings();
    expect(result.filename).toBe("holdings-20260902-051530Z.md");
    expect(await result.blob.text()).toContain("##### export");
  });
});

