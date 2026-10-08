import { expect, it, vi } from "vitest";
vi.mock("@/lib/og-font", () => ({ loadOgFont: vi.fn().mockResolvedValue(new ArrayBuffer(4)) }));
vi.mock("next/og", () => ({ ImageResponse: class extends Response {
  constructor(_element: React.ReactElement, options: { width: number; height: number; fonts: { data: ArrayBuffer }[] }) {
    super(JSON.stringify({ width: options.width, height: options.height, fonts: options.fonts.length }), { headers: { "Content-Type": "image/png" } });
  }
} }));
import { GET, dynamic, dynamicParams, generateStaticParams } from "./route";
import { loadOgFont } from "@/lib/og-font";
import { catalogs, type Locale } from "@/locales";
it("prerenders exactly three locale images and rejects unknown locales", async () => {
  expect(dynamic).toBe("force-static"); expect(dynamicParams).toBe(false);
  expect(generateStaticParams()).toEqual([{ locale: "en" }, { locale: "zh-Hans" }, { locale: "zh-Hant" }]);
  expect((await GET(new Request("https://portfonia.com/og/invalid"), { params: Promise.resolve({ locale: "invalid" }) })).status).toBe(404);
});
it.each(["en", "zh-Hans", "zh-Hant"] as Locale[])("renders a 1200x630 %s image using every rendered glyph", async (locale) => {
  const response = await GET(new Request(`https://portfonia.com/og/${locale}`), { params: Promise.resolve({ locale }) });
  expect(response.headers.get("content-type")).toBe("image/png");
  expect(await response.json()).toEqual({ width: 1200, height: 630, fonts: 1 });
  const copy = catalogs[locale].seo.og;
  expect(loadOgFont).toHaveBeenCalledWith(locale, ["Portfonia", copy.headline, copy.subline, copy.sampleTitle, ...copy.sampleRows].join(" "));
});
