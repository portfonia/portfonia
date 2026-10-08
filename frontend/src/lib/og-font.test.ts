import { afterEach, expect, it, vi } from "vitest";
import { loadOgFonts } from "./og-font";
afterEach(() => vi.unstubAllGlobals());
it("rejects a WOFF2-only stylesheet", async () => {
  const fetchMock = vi.fn(async () => new Response("src: url(https://fonts.test/subset.woff2) format('woff2');"));
  vi.stubGlobal("fetch", fetchMock);
  await expect(loadOgFonts("zh-Hans", "sample")).rejects.toThrow("No supported OpenType or TrueType font source");
});
it.each([
  ["en", ["Geist:wght@400", "Geist:wght@600", "Newsreader:wght@400"]],
  ["zh-Hans", ["Noto Sans SC:wght@400", "Noto Sans SC:wght@600", "Noto Serif SC:wght@400"]],
  ["zh-Hant", ["Noto Sans TC:wght@400", "Noto Sans TC:wght@600", "Noto Serif TC:wght@400"]],
] as const)("fetches %s sans 400/600 and serif 400 with the full glyph subset and OG User-Agent", async (locale, families) => {
  const binary = new Uint8Array([0, 1, 0, 0]).buffer;
  const fetchMock = vi.fn(async (input: string) => new Response(input.startsWith("https://fonts.googleapis.com/") ? "src: url(https://fonts.test/ignored.woff2) format('woff2'); src: url(https://fonts.test/subset.ttf) format('truetype');" : binary));
  vi.stubGlobal("fetch", fetchMock);
  expect(await loadOgFonts(locale, "Portfonia sample")).toEqual([
    { name: "Sans", data: binary, weight: 400, style: "normal" },
    { name: "Sans", data: binary, weight: 600, style: "normal" },
    { name: "Serif", data: binary, weight: 400, style: "normal" },
  ]);
  const cssCalls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).startsWith("https://fonts.googleapis.com/"));
  expect(cssCalls.map(([url]) => new URL(String(url)).searchParams.get("family"))).toEqual(families);
  for (const [url, options] of cssCalls) {
    expect(new URL(String(url)).searchParams.get("text")).toBe("Portfonia sample");
    expect(new Headers(options?.headers).get("User-Agent")).toContain("Version/5.0.5 Safari/533.21.1");
  }
  expect(fetchMock.mock.calls.filter(([url]) => url === "https://fonts.test/subset.ttf")).toHaveLength(3);
});
it("fails on either CSS or font HTTP errors", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 })));
  await expect(loadOgFonts("zh-Hans", "sample")).rejects.toThrow("Font stylesheet request failed: 503");
  vi.stubGlobal("fetch", vi.fn(async (input: string) => input.startsWith("https://fonts.googleapis.com/") ? new Response("src: url(https://fonts.test/subset.otf) format('opentype');") : new Response("unavailable", { status: 502 })));
  await expect(loadOgFonts("zh-Hans", "sample")).rejects.toThrow("Font binary request failed: 502");
});
