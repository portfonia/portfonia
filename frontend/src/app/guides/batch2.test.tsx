import { readFileSync } from "node:fs";
import { cleanup, render, within } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs } from "@/locales";
import { buildPageMetadata, isSeoPath, localizedPath } from "@/lib/seo";
import sitemap from "@/app/sitemap";
import { BENCHMARK_COLORS, PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
vi.mock("@supabase/ssr", () => ({ createServerClient: () => ({ auth: { getUser: async () => ({ data: { user: null } }) } }) }));
import { proxy } from "@/proxy";
const locales = ["en", "es", "zh-Hans", "zh-Hant"] as const;
const routes = ["/guides", "/guides/jade-stress", "/guides/jade-style", "/guides/jade-tracking"];
afterEach(cleanup);
it("739 A3 SEO paths and localization", () => {
  for (const path of routes) expect(isSeoPath(path), path).toBe(true);
  expect(localizedPath("/guides", "zh-Hant")).toBe("/zh-Hant/guides");
});
it("739 A4 anonymous access and locale rewrites", async () => {
  for (const [locale, path] of [["zh-Hans", "/guides"], ["es", "/guides/jade-style"]]) {
    const r = await proxy(new NextRequest(`https://portfonia.com/${locale}${path}`));
    expect(r.headers.get("x-middleware-rewrite")).toBe(`https://portfonia.com${path}`);
    expect(r.headers.get("x-middleware-request-x-portfonia-locale")).toBe(locale);
  }
  expect((await proxy(new NextRequest("https://portfonia.com/guides/jade-tracking"))).headers.get("location")).toBeNull();
});
it("739 A5 sixteen new sitemap URLs and fourteen alternates", () => {
  const entries = sitemap(); expect(entries).toHaveLength(52);
  for (const locale of locales) for (const path of routes) {
    const e = entries.find(e => e.url === `https://portfonia.com${localizedPath(path, locale)}`);
    expect(e).toBeDefined(); expect(Object.keys(e?.alternates?.languages ?? {})).toHaveLength(14);
  }
});
it("739 A6 index and stress metadata", () => {
  for (const [key, path] of [["guides", "/guides"], ["guideStress", "/guides/jade-stress"]] as const) {
    // The new keys must exist before the metadata function can render them.
    expect(catalogs["zh-Hant"].seo.pages).toHaveProperty(key);
    const m = buildPageMetadata(key as Parameters<typeof buildPageMetadata>[0], "zh-Hant");
    expect(m.alternates?.canonical).toBe(`https://portfonia.com/zh-Hant${path}`);
    expect(m.title).toBe((catalogs["zh-Hant"].seo.pages as Record<string, { title: string }>)[key].title);
    expect(m.description).toBe(catalogs["zh-Hant"].seo.pages[key].description);
  }
});
it.each(locales)("739 A7 %s server-readable guide sections, tables, captions and rendering", async locale => {
  const { Batch2Body } = await import("./_components/batch2-body");
  const { stressExample, styleExample, trackingExample } = await import("./_lib/example-batch2");
  const cases = [{ kind: "stress", example: stressExample() }, { kind: "style", example: styleExample() }, { kind: "tracking", example: trackingExample() }] as const;
  for (const props of cases) {
    window.history.replaceState(null, "", localizedPath(`/guides/jade-${props.kind}`, locale));
    const html = renderToStaticMarkup(<LocaleProvider routeLocale={locale}><Batch2Body {...props} /></LocaleProvider>);
    const doc = document.createElement("div");
    doc.innerHTML = html;
    const copy = (catalogs[locale].guides as unknown as Record<string, { title: string; sections: Record<string, { heading: string }> }>)[props.kind];
    expect(doc.querySelector("h1")?.textContent).toBe(copy.title);
    expect([...doc.querySelectorAll("h2")].map(e => e.textContent)).toEqual(Object.values(copy.sections).map(s => s.heading));
    expect(doc.querySelectorAll("table")).toHaveLength(props.kind === "style" ? 1 : 2);
    expect(doc.querySelectorAll("figcaption")).toHaveLength(props.kind === "tracking" ? 1 : 2);
    for (const caption of doc.querySelectorAll("caption,figcaption")) expect(caption.textContent).toContain(catalogs[locale].guides.common.exampleLabel);
    expect(doc.querySelector(`nav a[href="${localizedPath('/guides', locale)}"]`)).not.toBeNull();
    expect(doc.querySelector(`nav a[href="${localizedPath('/pricing', locale)}"]`)).not.toBeNull();
    const chart = doc.querySelector("[data-guide-chart]");
    expect(chart?.getAttribute("data-first-stroke")).toBe(PORTFOLIO_COLOR);
    expect(chart?.getAttribute("data-second-stroke")).toBe(props.kind === "style" ? "var(--muted-foreground)" : BENCHMARK_COLORS.sp500);
    if (props.kind === "stress") {
      expect(doc.querySelectorAll('button[aria-pressed="true"]')).toHaveLength(1);
      expect(doc.querySelectorAll("button:disabled")).toHaveLength(4);
      expect(within(doc).getByRole("button", { pressed: true, name: catalogs[locale].jade.stress.scenarios.tariffs_2025.name })).toBe(doc.querySelector('button[aria-pressed="true"]'));
      for (const id of ["dotcom_2000", "gfc_2008", "covid_2020", "rates_2022"] as const) {
        expect(within(doc).getByRole("button", { name: catalogs[locale].jade.stress.scenarios[id].name }).hasAttribute("disabled")).toBe(true);
      }
      const equity = doc.querySelector<HTMLElement>('[data-contribution="EQUITY_US_BROAD"] [data-bar]');
      const bond = doc.querySelector<HTMLElement>('[data-contribution="BOND_FUND"] [data-bar]');
      expect(equity?.style.right).toBe("50%"); expect(equity?.style.width).toBe("50%");
      expect(equity?.style.background).toBe("var(--destructive)"); expect(bond?.style.left).toBe("50%");
      expect(bond?.style.background).toBe(PORTFOLIO_COLOR);
      if (locale === "en") expect(doc.querySelector('[data-cell="shock-portfolio"]')?.textContent).toBe("-11.40%-$11,395");
    }
    if (locale === "en" && props.kind === "style") {
      expect(doc.querySelector('[data-cell="IWF-portfolio"]')?.textContent).toBe("29.14%");
      expect(doc.querySelector('[data-cell="BIL-spy"]')?.textContent).toBe("3.05%");
    }
    if (locale === "en" && props.kind === "tracking") expect(doc.querySelector('[data-cell="spy-fiveYear-trackingError"]')?.textContent).toBe("5.44%");
  }
});
it.each(locales)("739 A8 %s index card order and Pricing", async locale => {
  const { GuidesIndex } = await import("./_components/guides-index");
  window.history.replaceState(null, "", localizedPath("/guides", locale));
  const { container } = render(<LocaleProvider routeLocale={locale}><GuidesIndex /></LocaleProvider>);
  expect([...container.querySelectorAll('[data-guide-card] a')].map(e => e.getAttribute("href"))).toEqual(["replay", "var", "stress", "style", "tracking"].map(k => localizedPath(`/guides/jade-${k}`, locale)));
  expect(within(container.querySelector('main') as HTMLElement).getByRole('link', { name: catalogs[locale].guides.common.pricingLink })).toHaveAttribute('href', localizedPath('/pricing', locale));
});
function strings(v: unknown, p = ""): [string, string][] {
  return typeof v === "string" ? [[p, v]] : v && typeof v === "object" ? Object.entries(v).flatMap(([k, x]) => strings(x, p ? `${p}.${k}` : k)) : [];
}
it.each(locales)("739 A9 %s copy exists and uses descriptive vocabulary", locale => {
  for (const key of ["index", "stress", "style", "tracking"]) expect(catalogs[locale].guides).toHaveProperty(key);
  expect(catalogs[locale].jade.guideLinks).toHaveProperty("index");
  for (const [key, value] of strings({ guides: catalogs[locale].guides, lead: catalogs[locale].jade.guidesLead, links: catalogs[locale].jade.guideLinks })) {
    if (key === "guides.common.exampleLabel") continue;
    expect(value, key).not.toMatch(/\b(should|recommend\w*|buy|sell|hold|reduce|increase|exit|stop-loss|target price|entry point|oversold|overbought)\b|\u5efa\u8bae|\u5efa\u8b70|\u4e70\u5165|\u8cb7\u5165|\u5356\u51fa|\u8ce3\u51fa|recom\w*/i);
  }
});
it.each(["zh-Hans", "zh-Hant"] as const)("739 A10 %s punctuation and placeholder spacing", locale => {
  expect(catalogs[locale].guides).toHaveProperty("tracking");
  for (const [key, value] of strings({ guides: catalogs[locale].guides, lead: catalogs[locale].jade.guidesLead, links: catalogs[locale].jade.guideLinks })) {
    expect(value, key).not.toMatch(/[\u3002\uff0c\uff1a\uff1b] /);
    expect(value, key).not.toMatch(/[\u3400-\u9fff]\{|\}[\u3400-\u9fff]/);
  }
});
it("739 A12 llms lists all four new absolute URLs", () => {
  const text = readFileSync("public/llms.txt", "utf8");
  for (const path of routes) expect(text).toContain(`https://portfonia.com${path})`);
});
it.each(["stress", "style", "tracking"] as const)("739 A13 375px light and dark %s responsive figures and wrapping tables", async kind => {
  const { Batch2Body } = await import("./_components/batch2-body");
  const { stressExample, styleExample, trackingExample } = await import("./_lib/example-batch2");
  const props = kind === "stress" ? { kind, example: stressExample() } : kind === "style" ? { kind, example: styleExample() } : { kind, example: trackingExample() };
  for (const theme of ["light", "dark"]) {
    const { container } = render(<LocaleProvider routeLocale="en"><div style={{ width: 375 }} className={theme}><Batch2Body {...props} /></div></LocaleProvider>);
    for (const table of container.querySelectorAll("table")) expect(table).toHaveClass("w-full", "table-fixed");
    for (const chart of container.querySelectorAll("[data-guide-chart]")) expect(chart).toHaveClass("w-full", "min-w-0", "h-60");
    expect(container.querySelector('[class*="overflow-x"]')).toBeNull();
    expect(container.querySelector('.recharts-responsive-container')).not.toBeNull(); cleanup();
  }
});
