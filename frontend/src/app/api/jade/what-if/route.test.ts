// @vitest-environment node
import { afterEach, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const { token } = vi.hoisted(() => ({ token: vi.fn() }));
vi.mock("@/lib/supabase/server", () => ({ currentAccessToken: token }));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("804 A1 forwards the unchanged body, token and backend response", async () => {
  token.mockResolvedValue("session-token");
  const fetchMock = vi.fn().mockResolvedValue(new Response('{"result":true}', { status: 201 }));
  vi.stubGlobal("fetch", fetchMock);
  const { POST } = await import("./route");
  const body = '{ "versions": [] }';
  const response = await POST(new NextRequest("http://localhost/api/jade/what-if", { method: "POST", body }));
  expect(fetchMock).toHaveBeenCalledWith(`${process.env.BACKEND_URL ?? "http://localhost:8000"}/jade/what-if`, expect.objectContaining({ method: "POST", body, headers: { "content-type": "application/json", authorization: "Bearer session-token" }, cache: "no-store" }));
  expect(response.status).toBe(201);
  expect(await response.text()).toBe('{"result":true}');
});

it("804 A2 preserves the backend cooldown header", async () => {
  token.mockResolvedValue(null);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"detail":"what_if_cooldown"}', { status: 429, headers: { "Retry-After": "87" } })));
  const { POST } = await import("./route");
  const response = await POST(new NextRequest("http://localhost/api/jade/what-if", { method: "POST", body: "{}" }));
  expect(response.status).toBe(429);
  expect(response.headers.get("Retry-After")).toBe("87");
});

it("804 A3 maps an upstream timeout to 504 and passes an AbortSignal", async () => {
  token.mockResolvedValue(null);
  const timeout = vi.spyOn(AbortSignal, "timeout");
  const fetchMock = vi.fn().mockRejectedValue(new DOMException("expired", "TimeoutError"));
  vi.stubGlobal("fetch", fetchMock);
  const { POST } = await import("./route");
  const response = await POST(new NextRequest("http://localhost/api/jade/what-if", { method: "POST", body: "{}" }));
  expect(response.status).toBe(504);
  expect(await response.json()).toEqual({ detail: "Backend timeout" });
  expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  expect(timeout).toHaveBeenCalledWith(45_000);
});

it("804 A4 configures the shared rewrite timeout to 45 seconds", async () => {
  const { default: config } = await import("../../../../../next.config");
  expect(config.experimental?.proxyTimeout).toBe(45_000);
});
