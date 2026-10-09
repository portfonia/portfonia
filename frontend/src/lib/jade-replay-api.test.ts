import { afterEach, expect, it, vi } from "vitest";
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
import { getJadeReplay } from "./api";
afterEach(() => vi.unstubAllGlobals());
it("D5 API sends optional range without caching", async () => {
  const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response("{}")));
  vi.stubGlobal("fetch", fetch);
  await getJadeReplay(undefined, undefined, "1Y");
  expect(fetch).toHaveBeenLastCalledWith("/api/jade/replay?benchmark=sp500&range=1Y", { cache: "no-store" });
  await getJadeReplay("CNY", "csi300", "3M");
  expect(fetch).toHaveBeenLastCalledWith("/api/jade/replay?benchmark=csi300&base_currency=CNY&range=3M", { cache: "no-store" });
  await getJadeReplay();
  expect(fetch).toHaveBeenLastCalledWith("/api/jade/replay?benchmark=sp500", { cache: "no-store" });
});
