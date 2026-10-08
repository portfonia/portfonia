import { expect, it } from "vitest";
import sitemap from "./sitemap";
import { SEO_PAGES, SITE_URL, localizedPath } from "@/lib/seo";
import { type Locale } from "@/locales";
it("lists all 21 public URLs with eight absolute alternates and no invented date", () => {
  const entries = sitemap();
  expect(entries).toHaveLength(21);
  for (const path of Object.values(SEO_PAGES)) for (const locale of ["en", "zh-Hans", "zh-Hant"] as Locale[]) {
    const entry = entries.find((e) => e.url === SITE_URL + localizedPath(path, locale));
    expect(entry).toBeDefined();
    expect(entry).not.toHaveProperty("lastModified");
    expect(Object.keys(entry?.alternates?.languages ?? {})).toHaveLength(8);
    expect(entry?.alternates?.languages).toEqual({
      en: SITE_URL + path, "x-default": SITE_URL + path,
      "zh-CN": SITE_URL + localizedPath(path, "zh-Hans"), "zh-SG": SITE_URL + localizedPath(path, "zh-Hans"), zh: SITE_URL + localizedPath(path, "zh-Hans"),
      "zh-TW": SITE_URL + localizedPath(path, "zh-Hant"), "zh-HK": SITE_URL + localizedPath(path, "zh-Hant"), "zh-MO": SITE_URL + localizedPath(path, "zh-Hant"),
    });
  }
});
