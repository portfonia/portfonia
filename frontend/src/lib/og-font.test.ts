import { afterEach, expect, it, vi } from "vitest";
import { loadOgFont } from "./og-font";
afterEach(() => vi.unstubAllGlobals());
it("7d rejects a WOFF2-only stylesheet", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("src: url(https://fonts.test/subset.woff2) format('woff2');"));
  vi.stubGlobal("fetch", fetchMock);
  await expect(loadOgFont("zh-Hans", "sample")).rejects.toThrow("No supported OpenType or TrueType font source");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
it("7d fetches the TrueType source and requests the full glyph subset with the bundled OG User-Agent", async () => {
  const binary = new Uint8Array([0, 1, 0, 0]).buffer;
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response("src: url(https://fonts.test/ignored.woff2) format('woff2'); src: url(https://fonts.test/subset.ttf) format('truetype');")).mockResolvedValueOnce(new Response(binary));
  vi.stubGlobal("fetch", fetchMock);
  expect(await loadOgFont("zh-Hant", "Portfonia sample")).toEqual(binary);
  const url = new URL(fetchMock.mock.calls[0][0]);
  expect(url.searchParams.get("family")).toBe("Noto Sans TC");
  expect(url.searchParams.get("text")).toBe("Portfonia sample");
  expect(fetchMock.mock.calls[0][1].headers["User-Agent"]).toContain("Version/5.0.5 Safari/533.21.1");
  expect(fetchMock.mock.calls[1][0]).toBe("https://fonts.test/subset.ttf");
});
it("fails on either CSS or font HTTP errors", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 })));
  await expect(loadOgFont("zh-Hans", "sample")).rejects.toThrow("Font stylesheet request failed: 503");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response("src: url(https://fonts.test/subset.otf) format('opentype');")).mockResolvedValueOnce(new Response("unavailable", { status: 502 })));
  await expect(loadOgFont("zh-Hans", "sample")).rejects.toThrow("Font binary request failed: 502");
});
