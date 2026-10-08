import { Children, isValidElement, type ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { catalogs, type Locale } from "@/locales";

const captured = vi.hoisted(() => ({ element: null as ReactNode }));
vi.mock("@/lib/og-font", () => ({ loadOgFonts: vi.fn().mockResolvedValue([
  { name: "Sans", data: new ArrayBuffer(4), weight: 400, style: "normal" },
  { name: "Sans", data: new ArrayBuffer(4), weight: 600, style: "normal" },
  { name: "Serif", data: new ArrayBuffer(4), weight: 400, style: "normal" },
]) }));
vi.mock("next/og", () => ({ ImageResponse: class extends Response {
  constructor(element: ReactNode, options: { width: number; height: number; fonts: { data: ArrayBuffer }[] }) {
    captured.element = element;
    super(JSON.stringify({ width: options.width, height: options.height, fonts: options.fonts.length }), { headers: { "Content-Type": "image/png" } });
  }
} }));
import { GET, dynamic, dynamicParams, generateStaticParams } from "./route";
import { loadOgFonts } from "@/lib/og-font";

function strings(node: ReactNode): string[] {
  return Children.toArray(node).flatMap((child) => {
    if (typeof child === "string" || typeof child === "number") return [String(child)];
    return isValidElement<{ children?: ReactNode }>(child) ? strings(child.props.children) : [];
  });
}
beforeEach(() => vi.clearAllMocks());
it("prerenders exactly three locale images and rejects unknown locales", async () => {
  expect(dynamic).toBe("force-static"); expect(dynamicParams).toBe(false);
  expect(generateStaticParams()).toEqual([{ locale: "en" }, { locale: "zh-Hans" }, { locale: "zh-Hant" }]);
  expect((await GET(new Request("https://portfonia.com/og/invalid"), { params: Promise.resolve({ locale: "invalid" }) })).status).toBe(404);
  expect(loadOgFonts).not.toHaveBeenCalled();
});
it.each(["en", "zh-Hans", "zh-Hant"] as Locale[])("renders a 1200x630 %s image using three fonts and every rendered glyph", async (locale) => {
  const response = await GET(new Request(`https://portfonia.com/og/${locale}`), { params: Promise.resolve({ locale }) });
  expect(response.headers.get("content-type")).toBe("image/png");
  expect(await response.json()).toEqual({ width: 1200, height: 630, fonts: 3 });
  const c = catalogs[locale]; const h = c.home; const p = h.preview;
  const expected = [c.common.brandName, c.seo.og.headline, h.preview.tag, h.how.cards[0].title, ...c.seo.og.markets, h.how.cards[1].title, ...h.how.cards[1].tags, c.seo.og.sampleTitle, p.snapshotTitle, p.macroTitle, p.calendarTitle, p.analysisTitle, p.radarTitle, h.how.confidenceLead, ...Object.values(h.how.tiers)];
  expect(loadOgFonts).toHaveBeenCalledTimes(1);
  const [calledLocale, text] = vi.mocked(loadOgFonts).mock.calls[0];
  expect(calledLocale).toBe(locale);
  const rendered = strings(captured.element);
  for (const value of expected) { expect(text).toContain(value); expect(rendered).toContain(value); }
  for (const value of rendered.filter((v) => v.trim())) expect(text).toContain(value);
});
it("renders the five English sections in report order without the removed sample copy", async () => {
  await GET(new Request("https://portfonia.com/og/en"), { params: Promise.resolve({ locale: "en" }) });
  const p = catalogs.en.home.preview;
  const sectionTitles = [p.snapshotTitle, p.macroTitle, p.calendarTitle, p.analysisTitle, p.radarTitle];
  const rendered = strings(captured.element);
  expect(rendered.filter((value) => sectionTitles.includes(value))).toEqual(sectionTitles);
  for (const old of ["Daily briefings across brokers and markets — US, Hong Kong, China A-shares and more.", "Illustrative company: quarterly results published.", "Macro calendar: inflation release this week.", "Portfolio context: exposure across markets."]) expect(rendered).not.toContain(old);
});
