import { NextRequest } from "next/server";
import { describe, expect, it, vi } from "vitest";

const { getUser, getSession, createServerClient } = vi.hoisted(() => ({
  getUser: vi.fn(),
  getSession: vi.fn(),
  createServerClient: vi.fn(),
}));

vi.mock("@supabase/ssr", () => ({
  createServerClient: (
    _url: string,
    _key: string,
    opts: {
      cookies: {
        setAll?: (
          cookies: { name: string; value: string; options?: Record<string, unknown> }[],
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
          // adapter to queue the refreshed session cookie.
          opts.cookies.setAll?.([
            { name: "sb-refreshed-session", value: "new-token-value", options: { path: "/" } },
          ]);
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

  it.each(["/", "/login", "/signup?invite=abc"])(
    "never redirects the public route %s even when unauthenticated",
    async (path) => {
      getUser.mockResolvedValue({ data: { user: null } });
      getSession.mockResolvedValue({ data: { session: null } });

      const res = await proxy(makeRequest(path));

      expect(res.headers.get("location")).toBeNull();
    },
  );

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
});
