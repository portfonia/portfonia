import { expect, it } from "vitest";
import sitemap from "./sitemap";
import { SEO_PAGES, SITE_URL, localizedPath } from "@/lib/seo";
import { type Locale } from "@/locales";
it("lists all 56 public URLs with fourteen absolute alternates and no invented date", () => {
  const entries = sitemap();
  expect(entries).toHaveLength(56);
  for (const path of Object.values(SEO_PAGES)) for (const locale of ["en", "es", "zh-Hans", "zh-Hant"] as Locale[]) {
    const entry = entries.find((e) => e.url === SITE_URL + localizedPath(path, locale));
    expect(entry).toBeDefined();
    expect(entry).not.toHaveProperty("lastModified");
    expect(Object.keys(entry?.alternates?.languages ?? {})).toHaveLength(14);
    expect(entry?.alternates?.languages).toEqual({
      en: SITE_URL + path,
      es: SITE_URL + localizedPath(path, "es"), "es-419": SITE_URL + localizedPath(path, "es"), "es-US": SITE_URL + localizedPath(path, "es"), "es-MX": SITE_URL + localizedPath(path, "es"), "x-default": SITE_URL + path,
      "zh-Hans": SITE_URL + localizedPath(path, "zh-Hans"), "zh-CN": SITE_URL + localizedPath(path, "zh-Hans"), "zh-SG": SITE_URL + localizedPath(path, "zh-Hans"), zh: SITE_URL + localizedPath(path, "zh-Hans"),
      "zh-Hant": SITE_URL + localizedPath(path, "zh-Hant"), "zh-TW": SITE_URL + localizedPath(path, "zh-Hant"), "zh-HK": SITE_URL + localizedPath(path, "zh-Hant"), "zh-MO": SITE_URL + localizedPath(path, "zh-Hant"),
    });
  }
});

it("acceptance_07 sitemap has 56 URLs with fourteen alternates including es-419", () => {
  const entries = sitemap();
  expect(entries).toHaveLength(56);
  for (const entry of entries) {
    expect(Object.keys(entry.alternates?.languages ?? {})).toHaveLength(14);
    const path = new URL(entry.url).pathname.replace(/^\/(es|zh-Hans|zh-Hant)(?=\/|$)/, "") || "/";
    expect(entry.alternates?.languages?.["es-419"]).toBe(`https://portfonia.com/es${path === "/" ? "" : path}`);
  }
});
