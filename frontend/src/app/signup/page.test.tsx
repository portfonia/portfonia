// @vitest-environment node
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { headerStore } = vi.hoisted(() => ({
  headerStore: new Map<string, string>(),
}));

vi.mock("next/headers", () => ({
  headers: async () => ({
    get: (name: string) => headerStore.get(name.toLowerCase()) ?? null,
  }),
}));
vi.mock("./signup-heading", () => ({ SignupHeading: () => null }));
vi.mock("./signup-form", () => ({ SignupForm: () => null }));

import SignupPage from "./page";

const originalFetch = global.fetch;

describe("signup page invite-email lookup", () => {
  afterEach(() => {
    global.fetch = originalFetch;
    headerStore.clear();
    vi.resetAllMocks();
  });

  it("forwards the visitor IP and passes the bound email to the form", async () => {
    headerStore.set("x-forwarded-for", "203.0.113.50, 10.0.0.2");
    headerStore.set("x-real-ip", "203.0.113.50");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ email: "bound@example.com" }), { status: 200 }),
    );
    global.fetch = fetchMock;

    const page = await SignupPage({ searchParams: Promise.resolve({ invite: "a+b/c" }) });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/auth/invite-email?token=a%2Bb%2Fc");
    expect(init.cache).toBe("no-store");
    const forwarded = new Headers(init.headers);
    expect(forwarded.get("x-forwarded-for")).toBe("203.0.113.50, 10.0.0.2");
    expect(forwarded.get("x-real-ip")).toBe("203.0.113.50");
    const children = page.props.children as ReactElement<{ lockedEmail: string | null }>[];
    expect(children[1].props.lockedEmail).toBe("bound@example.com");
  });

  it("omits IP headers when the ingress did not provide them", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ email: null }), { status: 200 }),
    );
    global.fetch = fetchMock;

    await SignupPage({ searchParams: Promise.resolve({ invite: "token" }) });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const forwarded = new Headers(init.headers);
    expect(forwarded.has("x-forwarded-for")).toBe(false);
    expect(forwarded.has("x-real-ip")).toBe(false);
  });
});
