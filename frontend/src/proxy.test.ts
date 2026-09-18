import { NextRequest } from "next/server";
import { describe, expect, it, vi } from "vitest";

const { getUser, getSession, createServerClient } = vi.hoisted(() => ({
  getUser: vi.fn(),
  getSession: vi.fn(),
  createServerClient: vi.fn(),
}));

// The exact headers @supabase/ssr 0.12.4 passes as setAll's second argument
// whenever it writes auth cookies (verified against
// node_modules/@supabase/ssr/dist/module/cookies.js — not invented).
const REFRESH_HEADERS = {
  "Cache-Control": "private, no-cache, no-store, must-revalidate, max-age=0",
  Expires: "0",
  Pragma: "no-cache",
};

vi.mock("@supabase/ssr", () => ({
  createServerClient: (
    _url: string,
    _key: string,
    opts: {
      cookies: {
        setAll?: (
          cookies: { name: string; value: string; options?: Record<string, unknown> }[],
          headers: Record<string, string>,
        ) => void;
      };
    },
  ) => {
    createServerClient(opts);
    return {
      auth: {
        getUser: async () => {
          // Simulate @supabase/ssr's real behavior: getUser() is what
          // triggers a token refresh and invokes the cookies.setAll
          // adapter to queue the refreshed session cookie, along with the
          // cache-prevention headers the real library always sends here.
          opts.cookies.setAll?.(
            [{ name: "sb-refreshed-session", value: "new-token-value", options: { path: "/" } }],
            REFRESH_HEADERS,
          );
          return getUser();
        },
        getSession,
      },
    };
  },
}));

import { proxy } from "./proxy";

function makeRequest(path: string) {
  return new NextRequest(new URL(path, "https://portfonia.com"));
}

const AUTHED_USER = { id: "11111111-1111-1111-1111-111111111111" };
const ACCESS_TOKEN = "sb-access-token-abc";

describe("proxy", () => {
  it("redirects an unauthenticated request to a protected route to /login", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/holdings"));

    expect(res.status).toBe(307);
    expect(new URL(res.headers.get("location")!).pathname).toBe("/login");
  });

  it("does not redirect an authenticated request to a protected route", async () => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    getSession.mockResolvedValue({
      data: { session: { access_token: ACCESS_TOKEN } },
    });

    const res = await proxy(makeRequest("/holdings"));

    expect(res.headers.get("location")).toBeNull();
  });

  it.each([
    "/",
    "/login",
    "/signup?invite=abc",
    "/forgot-password",
    "/reset-password",
    "/verify-email",
    "/unsubscribe",
    "/terms",
    "/privacy",
    "/altcha.js",
  ])(
    "never redirects the public route %s even when unauthenticated",
    async (path) => {
      getUser.mockResolvedValue({ data: { user: null } });
      getSession.mockResolvedValue({ data: { session: null } });

      const res = await proxy(makeRequest(path));

      expect(res.headers.get("location")).toBeNull();
    },
  );

  // Issue #453: these three exact page routes must work even during a
  // Supabase/Auth outage — a recipient's confirm/retrieve/revoke link must
  // never depend on the Auth provider being reachable. Exempted from the
  // Supabase lookup entirely (not just from the redirect check), so
  // createServerClient/getUser is never even called for them.
  it.each(["/vigil/confirm", "/vigil/retrieve", "/vigil/revoke"])(
    "never calls the Supabase client for the public Vigil shell %s, even unauthenticated",
    async (path) => {
      getUser.mockClear();
      createServerClient.mockClear();

      const res = await proxy(makeRequest(path));

      expect(res.headers.get("location")).toBeNull();
      expect(getUser).not.toHaveBeenCalled();
      expect(createServerClient).not.toHaveBeenCalled();
    },
  );

  // blacktomb42 review, PR #506: no trailingSlash config in next.config.ts
  // means Next defaults to trailingSlash: false, and does NOT normalize
  // the pathname before middleware runs — request.nextUrl.pathname carries
  // the trailing slash exactly as sent. An exact-match array (.includes)
  // would silently miss "/vigil/confirm/" and send a recipient through the
  // normal Supabase/login-redirect path instead of the exemption.
  it.each(["/vigil/confirm/", "/vigil/retrieve/", "/vigil/revoke/"])(
    "still exempts the public Vigil shell %s with a trailing slash",
    async (path) => {
      getUser.mockClear();
      createServerClient.mockClear();

      const res = await proxy(makeRequest(path));

      expect(res.headers.get("location")).toBeNull();
      expect(getUser).not.toHaveBeenCalled();
      expect(createServerClient).not.toHaveBeenCalled();
    },
  );

  it("still sets ?next=/vigil when the originally-requested path has a trailing slash", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/vigil/"));

    const location = new URL(res.headers.get("location")!);
    expect(location.searchParams.get("next")).toBe("/vigil");
  });

  it("does NOT exempt /vigil/setup — an unauthenticated request still redirects to /login", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/vigil/setup"));

    expect(res.status).toBe(307);
    expect(new URL(res.headers.get("location")!).pathname).toBe("/login");
  });

  it("does NOT exempt the /vigil dashboard itself — an unauthenticated request still redirects to /login", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/vigil"));

    expect(res.status).toBe(307);
    expect(new URL(res.headers.get("location")!).pathname).toBe("/login");
  });

  // The literal "/vigil" return target is hardcoded by proxy.ts itself,
  // never echoed from attacker-controlled input — this is what makes A03's
  // "reject any other/external/protocol-relative return URL" hold: there is
  // no code path that could ever emit anything else here.
  it("appends ?next=/vigil to the /login redirect only when the originally-requested path was exactly /vigil", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/vigil"));

    const location = new URL(res.headers.get("location")!);
    expect(location.searchParams.get("next")).toBe("/vigil");
  });

  it("does not set ?next= on the /login redirect for any other protected route", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/holdings"));

    const location = new URL(res.headers.get("location")!);
    expect(location.searchParams.has("next")).toBe(false);
  });

  it("never calls the Supabase client for /api/vigil/public/* even unauthenticated", async () => {
    getUser.mockClear();

    const res = await proxy(makeRequest("/api/vigil/public/confirm"));

    expect(res.headers.get("location")).toBeNull();
    expect(getUser).not.toHaveBeenCalled();
  });

  it("never calls the Supabase client for POST /api/vigil/webhooks/resend", async () => {
    getUser.mockClear();

    const res = await proxy(makeRequest("/api/vigil/webhooks/resend"));

    expect(res.headers.get("location")).toBeNull();
    expect(getUser).not.toHaveBeenCalled();
  });

  it("never redirects a same-origin /api/* request even when unauthenticated (the backend enforces 401 itself)", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/api/holdings"));

    expect(res.headers.get("location")).toBeNull();
  });

  it("injects Authorization: Bearer <access_token> on the forwarded request for an authenticated /api/* call", async () => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    getSession.mockResolvedValue({
      data: { session: { access_token: ACCESS_TOKEN } },
    });

    const res = await proxy(makeRequest("/api/holdings"));

    // NextResponse.next({ request: { headers } }) surfaces the rewritten
    // request headers via this response header (Next's own mechanism for
    // "headers available upstream" — see next-response#next docs).
    expect(res.headers.get("x-middleware-request-authorization")).toBe(
      `Bearer ${ACCESS_TOKEN}`,
    );
  });

  // Next only actually applies a header override if it's listed in
  // x-middleware-override-headers — checking the x-middleware-request-*
  // value alone (as the round-1 version of this test did) stays green
  // even when that list gets clobbered and the header is silently never
  // applied (PR #185 round-2 review: this is the exact regression class
  // that slipped through the round-1 test).
  it("lists authorization in x-middleware-override-headers", async () => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    getSession.mockResolvedValue({
      data: { session: { access_token: ACCESS_TOKEN } },
    });

    const res = await proxy(makeRequest("/api/holdings"));

    const overridden = (res.headers.get("x-middleware-override-headers") ?? "")
      .split(",")
      .map((s) => s.trim());
    expect(overridden).toContain("authorization");
  });

  it("sets no Authorization header for an unauthenticated /api/* call", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/api/holdings"));

    expect(res.headers.get("x-middleware-request-authorization")).toBeNull();
  });

  it("keeps a refreshed session cookie (from getUser()'s setAll) on the final response even when the Authorization-injection branch rebuilds it for an /api/* request", async () => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    getSession.mockResolvedValue({
      data: { session: { access_token: ACCESS_TOKEN } },
    });

    const res = await proxy(makeRequest("/api/holdings"));

    const setCookie = res.cookies.get("sb-refreshed-session");
    expect(setCookie?.value).toBe("new-token-value");
    // And the Authorization header must still be set — this isn't an
    // either/or.
    expect(res.headers.get("x-middleware-request-authorization")).toBe(
      `Bearer ${ACCESS_TOKEN}`,
    );
  });

  it("applies the cache-prevention headers @supabase/ssr passes to setAll onto the response (a stale CDN/proxy cache must never serve one user's session to another)", async () => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    getSession.mockResolvedValue({
      data: { session: { access_token: ACCESS_TOKEN } },
    });

    const res = await proxy(makeRequest("/holdings"));

    for (const [key, value] of Object.entries(REFRESH_HEADERS)) {
      expect(res.headers.get(key)).toBe(value);
    }
  });

  it("keeps those cache-prevention headers on the final response even when the Authorization-injection branch rebuilds it for an /api/* request", async () => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    getSession.mockResolvedValue({
      data: { session: { access_token: ACCESS_TOKEN } },
    });

    const res = await proxy(makeRequest("/api/holdings"));

    for (const [key, value] of Object.entries(REFRESH_HEADERS)) {
      expect(res.headers.get(key)).toBe(value);
    }
  });
});
