import { NextRequest } from "next/server";
import { describe, expect, it, vi } from "vitest";

const { getUser, getSession, createServerClient } = vi.hoisted(() => ({
  getUser: vi.fn(),
  getSession: vi.fn(),
  createServerClient: vi.fn(),
}));

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
  return new NextRequest(new URL(path, "https://vigil.portfonia.com"));
}

const AUTHED_USER = { id: "11111111-1111-1111-1111-111111111111" };
const ACCESS_TOKEN = "sb-access-token-abc";

describe("proxy", () => {
  it.each(["/", "/c", "/r", "/login"])(
    "does not redirect unauthenticated %s (confirm/retrieve must stay public)",
    async (path) => {
      getUser.mockResolvedValue({ data: { user: null } });
      getSession.mockResolvedValue({ data: { session: null } });

      const res = await proxy(makeRequest(path));

      expect(res.headers.get("location")).toBeNull();
      expect(res.status).not.toBe(307);
    },
  );

  it("injects Authorization: Bearer <access_token> for an authenticated /api/* call", async () => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    getSession.mockResolvedValue({
      data: { session: { access_token: ACCESS_TOKEN } },
    });

    const res = await proxy(makeRequest("/api/vault"));

    expect(res.headers.get("x-middleware-request-authorization")).toBe(
      `Bearer ${ACCESS_TOKEN}`,
    );
  });

  it("sets no Authorization header for an unauthenticated /api/* call", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    getSession.mockResolvedValue({ data: { session: null } });

    const res = await proxy(makeRequest("/api/vault"));

    expect(res.headers.get("x-middleware-request-authorization")).toBeNull();
  });

  it("does not set a parent-domain cookie when refreshing the session", async () => {
    getUser.mockResolvedValue({ data: { user: AUTHED_USER } });
    getSession.mockResolvedValue({
      data: { session: { access_token: ACCESS_TOKEN } },
    });

    const res = await proxy(makeRequest("/"));
    const cookie = res.cookies.get("sb-refreshed-session");
    expect(cookie?.value).toBe("new-token-value");
    expect(JSON.stringify(cookie)).not.toContain(".portfonia.com");
  });
});
