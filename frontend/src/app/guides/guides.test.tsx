import { readFileSync } from "node:fs";
import { cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { catalogs } from "@/locales";
import { isSeoPath, localizedPath, buildPageMetadata } from "@/lib/seo";
import sitemap from "@/app/sitemap";
vi.mock("@supabase/ssr", () => ({ createServerClient: () => ({ auth: { getUser: async () => ({ data: { user: null } }) } }) }));
import { proxy } from "@/proxy";
const locales = ["en", "es", "zh-Hans", "zh-Hant"] as const;
afterEach(cleanup);
function strings(v: unknown, path = ""): [
  string,
  string
][] {
  if (typeof v === "string")
    return [[path, v]];
  if (v && typeof v === "object")
    return Object.entries(v).flatMap(([k, x]) => strings(x, path ? `${path}.${k}` : k));
  return [];
}

it("737 A2 guide SEO registration and localized paths", () => {
  expect(isSeoPath("/guides/jade-replay")).toBe(true);
  expect(isSeoPath("/guides/jade-var")).toBe(true);
  expect(localizedPath("/guides/jade-var", "zh-Hans")).toBe("/zh-Hans/guides/jade-var");
});

it("737 A3 anonymous pass and locale rewrites", async () => {
  for (const [path, target, locale] of [["/zh-Hant/guides/jade-var", "/guides/jade-var", "zh-Hant"], ["/es/guides/jade-replay", "/guides/jade-replay", "es"]]) {
    const r = await proxy(new NextRequest(`https://portfonia.com${path}`));
    expect(r.headers.get("x-middleware-rewrite")).toBe(`https://portfonia.com${target}`);
    expect(r.headers.get("x-middleware-request-x-portfonia-locale")).toBe(locale);
  }
  const r = await proxy(new NextRequest("https://portfonia.com/guides/jade-var"));
  expect(r.headers.get("location")).toBeNull();
});

it("737 A4 all eight guide sitemap URLs with fourteen alternates", () => {
  const entries = sitemap();
  expect(entries).toHaveLength(52);
  for (const locale of locales)
    for (const path of ["/guides/jade-replay", "/guides/jade-var"]) {
      const row = entries.find(e => e.url === `https://portfonia.com${localizedPath(path, locale)}`);
      expect(row).toBeDefined();
      if (!row?.alternates?.languages)
        throw new Error("Missing sitemap language alternates");
      expect(Object.keys(row.alternates.languages)).toHaveLength(14);
    }
});

it("737 A5 Spanish metadata", () => {
  const m = buildPageMetadata("guideVar", "es");
  expect(m.alternates?.canonical).toBe("https://portfonia.com/es/guides/jade-var");
  expect(m.title).toBe(catalogs.es.seo.pages.guideVar.title);
  expect(m.description).toBe(catalogs.es.seo.pages.guideVar.description);
});

it.each(locales)("737 A7 %s copy vocabulary with exactly one disclaimer exception", locale => {
  expect(catalogs[locale]).toHaveProperty("guides");
  for (const [key, value] of strings(catalogs[locale].guides))
    if (key !== "common.exampleLabel")
      expect(value, key).not.toMatch(/\b(should|recommend\w*|buy|sell)\b|\u5efa\u8bae|\u5efa\u8b70|\u4e70\u5165|\u8cb7\u5165|\u5356\u51fa|\u8ce3\u51fa|recom\w*/i);
  expect(catalogs.en.guides.common.exampleLabel).toBe("Example portfolio, not a recommendation");
});

it.each(["zh-Hans", "zh-Hant"] as const)("737 A8 %s punctuation", locale => {
  expect(catalogs[locale]).toHaveProperty("guides");
  const c = catalogs[locale];
  for (const [key, value] of strings({
    guides: c.guides,
    jade: {
      guideLink: c.jade.guideLink,
      guidesLead: c.jade.guidesLead,
      guideLinks: c.jade.guideLinks
    }
  }))
    expect(value, key).not.toMatch(/[\u3002\uff0c\uff1a\uff1b] /);
});

it.each(locales)("737 A10 %s only final intro sentence removed", locale => {
  const old = ({
    "en": "Jade is a set of portfolio risk tools for long-term holders. It works from the holdings you have today, and every result states the data it used and how it was calculated. A Jade subscription includes everything in the Advanced plan, and you can choose any briefing schedule and change it at any time. Risk tools are added to this page one at a time.",
    "es": "Jade re\u00fane herramientas de riesgo de cartera para inversores a largo plazo. Parte de sus posiciones actuales y cada resultado indica los datos utilizados y el c\u00e1lculo. Incluye todo el plan Advanced; puede elegir cualquier calendario de res\u00famenes y cambiarlo cuando quiera. Las herramientas se a\u00f1aden a esta p\u00e1gina una a una.",
    "zh-Hans": "\u6da6\u7389\u662f\u4e00\u7ec4\u9762\u5411\u957f\u671f\u6301\u4ed3\u8005\u7684\u6295\u8d44\u7ec4\u5408\u98ce\u9669\u5de5\u5177\uff0c\u4ee5\u60a8\u5f53\u524d\u7684\u6301\u4ed3\u4e3a\u57fa\u7840\uff0c\u6bcf\u9879\u7ed3\u679c\u90fd\u6ce8\u660e\u6240\u7528\u6570\u636e\u548c\u8ba1\u7b97\u65b9\u6cd5\u3002\u6da6\u7389\u8ba2\u9605\u5305\u542b\u8fdb\u9636\u5957\u9910\u7684\u5168\u90e8\u529f\u80fd\uff0c\u60a8\u53ef\u4ee5\u9009\u62e9\u4efb\u4e00\u7b80\u62a5\u9891\u7387\uff0c\u5e76\u968f\u65f6\u8c03\u6574\u3002\u98ce\u9669\u5de5\u5177\u5c06\u9010\u9879\u52a0\u5165\u672c\u9875\u9762\u3002",
    "zh-Hant": "\u6f64\u7389\u63d0\u4f9b\u4e00\u7cfb\u5217\u4f9b\u9577\u671f\u6301\u5009\u8005\u4f7f\u7528\u7684\u6295\u8cc7\u7d44\u5408\u98a8\u96aa\u5de5\u5177\uff0c\u4ee5\u60a8\u76ee\u524d\u7684\u6301\u5009\u9032\u884c\u8a08\u7b97\uff0c\u4e26\u5728\u6bcf\u9805\u7d50\u679c\u4e2d\u5217\u660e\u8cc7\u6599\u4f86\u6e90\u8207\u8a08\u7b97\u65b9\u5f0f\u3002\u6f64\u7389\u8a02\u95b1\u6db5\u84cb\u9032\u968e\u65b9\u6848\u7684\u6240\u6709\u529f\u80fd\uff0c\u7c21\u5831\u6392\u7a0b\u7531\u60a8\u9078\u64c7\uff0c\u4e5f\u80fd\u96a8\u6642\u8b8a\u66f4\u3002\u98a8\u96aa\u5de5\u5177\u6703\u9678\u7e8c\u52a0\u5165\u6b64\u9801\u9762\u3002"
  })[locale];
  const finalStart = locale.startsWith('zh') ? old.lastIndexOf('\u3002', old.length - 2) + 1 : old.lastIndexOf('. ', old.length - 2) + 2;
  expect(catalogs[locale].jade.intro).toBe(old.slice(0, finalStart).trimEnd());
});

it("737 A11 llms absolute URLs", () => {
  const text = readFileSync('public/llms.txt', 'utf8');
  expect(text).toContain('https://portfonia.com/guides/jade-replay');
  expect(text).toContain('https://portfonia.com/guides/jade-var');
});

it("737 fix round Chinese example labels match owner wording", () => {
  expect(catalogs["zh-Hans"].guides.common.exampleLabel).toBe("\u793a\u4f8b\u7ec4\u5408\uff0c\u975e\u63a8\u8350");
  expect(catalogs["zh-Hant"].guides.common.exampleLabel).toBe("\u793a\u4f8b\u7d44\u5408\uff0c\u975e\u63a8\u85a6");
});

it.each(["zh-Hans", "zh-Hant"] as const)("737 fix round %s placeholders have CJK spacing", locale => {
  for (const [key, value] of strings(catalogs[locale].guides)) {
    expect(value, key).not.toMatch(/[\u3400-\u9fff]\{|\}[\u3400-\u9fff]/);
  }
});

it("737 fix round 2 zh-Hans guide terminology matches Jade", () => {
  for (const [key, value] of strings(catalogs["zh-Hans"].guides)) {
    expect(value, key).not.toContain("\u5728\u9669\u4ef7\u503c");
  }
});
