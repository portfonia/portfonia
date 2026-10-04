// @vitest-environment node
import { afterEach, expect, it, vi } from "vitest";
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("@/lib/supabase/server", () => ({ currentAccessToken: () => Promise.resolve("fixture-token") }));
import { listReports } from "./api";
import { getReportsServer, getReportServer } from "./server-api";
afterEach(() => vi.unstubAllGlobals());
it("report client forwards the same URL filters to the API proxy", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 })); vi.stubGlobal("fetch", fetchMock);
  const query = { page: "2", sort: "asc", kind: "report", date_from: "2026-10-01", date_to: "2026-10-03" };
  await listReports(query);
  const url = new URL(String(fetchMock.mock.calls[0][0]), "https://fixture.example");
  expect(url.pathname).toBe("/api/reports"); expect(Object.fromEntries(url.searchParams)).toEqual(query);
});
it("SSR uses the canonical list URL and carries the session bearer", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200 })); vi.stubGlobal("fetch", fetchMock);
  await getReportsServer();
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(new URL(url).pathname).toBe("/reports"); expect(new Headers(init.headers).get("authorization")).toBe("Bearer fixture-token"); expect(init.cache).toBe("no-store");
});
it("SSR distinguishes missing report from backend failure", async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response("", { status: 404 })).mockResolvedValueOnce(new Response("", { status: 500 })); vi.stubGlobal("fetch", fetchMock);
  expect(await getReportServer("missing")).toBeNull(); await expect(getReportServer("failed")).rejects.toThrow("500");
});
