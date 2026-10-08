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
    "/waitlist",
    "/reset-password",
    "/verify-email",
    "/unsubscribe",
    "/terms",
    "/privacy",
    "/pricing",
    "/refund",
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

  it("does not set ?next= on the /login redirect for any protected route", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/holdings"));

    const location = new URL(res.headers.get("location")!);
    expect(location.searchParams.has("next")).toBe(false);
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


describe("issue #652 agent-readable documentation", () => {
  it.each(["/llms.txt", "/agent.md"])("acceptance_08 serves %s without sign-in", async (path) => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });
    const res = await proxy(makeRequest(path));
    expect(res.status).toBe(200);
    expect(res.headers.get("location")).toBeNull();
  });
  it("acceptance_08 llms.txt links the published English API reference", async () => {
    const { readFile } = await import("node:fs/promises");
    const text = await readFile(`${process.cwd()}/public/llms.txt`, "utf8");
    expect(text).toContain("https://portfonia.com/agent.md");
    expect(text).toContain("https://portfonia.com/agent");
  });
});


describe("issue #702 public SEO routing", () => {
  it.each([["/zh-Hans/pricing", "/pricing", "zh-Hans"], ["/zh-Hant", "/", "zh-Hant"], ["/zh-Hant/about", "/about", "zh-Hant"]])("rewrites %s with locale and refreshed session", async (path, target, locale) => {
    getUser.mockResolvedValue({ data: { user: null } });
    const res = await proxy(makeRequest(path));
    expect(res.headers.get("location")).toBeNull();
    expect(res.headers.get("x-middleware-rewrite")).toBe(`https://portfonia.com${target}`);
    expect(res.headers.get("x-middleware-request-x-portfonia-locale")).toBe(locale);
    expect(res.headers.get("x-middleware-override-headers")?.split(",")).toContain("x-portfonia-locale");
    expect(res.cookies.get("sb-refreshed-session")?.value).toBe("new-token-value");
    for (const [key, value] of Object.entries(REFRESH_HEADERS)) expect(res.headers.get(key)).toBe(value);
  });
  it.each(["/zh-Hans/login", "/zh-Hans/careers", "/zh-Hans/holdings"])("forwards locale for the unmatched path %s without rewriting", async (path) => {
    getUser.mockResolvedValue({ data: { user: null } });
    const res = await proxy(makeRequest(path));
    expect(res.headers.get("location")).toBeNull();
    expect(res.headers.get("x-middleware-rewrite")).toBeNull();
    expect(res.headers.get("x-middleware-request-x-portfonia-locale")).toBe("zh-Hans");
    expect(res.headers.get("x-middleware-override-headers")?.split(",")).toContain("x-portfonia-locale");
    expect(res.cookies.get("sb-refreshed-session")?.value).toBe("new-token-value");
    for (const [key, value] of Object.entries(REFRESH_HEADERS)) expect(res.headers.get(key)).toBe(value);
  });
  it.each(["/about", "/careers", "/robots.txt", "/sitemap.xml", "/og/en", "/holdingsx"])("passes anonymous %s through", async (path) => {
    getUser.mockResolvedValue({ data: { user: null } });
    expect((await proxy(makeRequest(path))).headers.get("location")).toBeNull();
  });
  it.each(["/holdings", "/portfolio/x", "/reports", "/profile", "/questionnaire", "/welcome"])("keeps anonymous %s protected", async (path) => {
    getUser.mockResolvedValue({ data: { user: null } });
    const res = await proxy(makeRequest(path));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("https://portfonia.com/login");
  });
  it("does not redirect an unknown signed-in URL", async () => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    expect((await proxy(makeRequest("/careers"))).headers.get("location")).toBeNull();
  });
});


describe("issue #702 follow-up header and path boundaries", () => {
  it.each(["/%68oldings", "//holdings", "/%2Fholdings", "/holdings%2F..%2Fcareers"])("normalizes %s before protection", async (path) => {
    getUser.mockResolvedValue({ data: { user: null } });
    // A network-path reference must not change the URL host in this fixture.
    const request = new NextRequest(`https://portfonia.com${path}`);
    const res = await proxy(request);
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("https://portfonia.com/login");
  });
  it.each(["/reports-sample", "/%2568oldings", "/zh-Hans/holdings", "/bad%escape", "/missing%2f..%2fholdings"])("7g leaves %s unprotected without a second decode or dot-segment resolution", async (path) => {
    getUser.mockResolvedValue({ data: { user: null } });
    expect((await proxy(new NextRequest(`https://portfonia.com${path}`))).headers.get("location")).toBeNull();
  });
  it.each([["/pricing", null], ["/login", null], ["/api/holdings", null], ["/zh-Hant/pricing", "zh-Hant"]])("trusts only the URL locale on %s and forwards the refreshed cookie", async (path, expected) => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    getSession.mockResolvedValue({ data: { session: { access_token: ACCESS_TOKEN } } });
    const res = await proxy(new NextRequest(`https://portfonia.com${path}`, { headers: { "x-portfonia-locale": "zh-Hans" } }));
    expect(res.headers.get("x-middleware-request-x-portfonia-locale")).toBe(expected);
    const keys = res.headers.get("x-middleware-override-headers")?.split(",") ?? [];
    if (expected) expect(keys).toContain("x-portfonia-locale"); else expect(keys).not.toContain("x-portfonia-locale");
    expect(res.headers.get("x-middleware-request-cookie")).toContain("sb-refreshed-session=new-token-value");
    expect(res.cookies.get("sb-refreshed-session")?.value).toBe("new-token-value");
    for (const [key, value] of Object.entries(REFRESH_HEADERS)) expect(res.headers.get(key)).toBe(value);
  });
});
