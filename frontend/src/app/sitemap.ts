import type { MetadataRoute } from "next";
import { SEO_PAGES, SITE_URL, languageAlternates, localizedPath } from "@/lib/seo";
import { type Locale } from "@/locales";
export default function sitemap(): MetadataRoute.Sitemap {
  return Object.values(SEO_PAGES).flatMap((path) => (["en", "zh-Hans", "zh-Hant"] as Locale[]).map((locale) => ({ url: SITE_URL + localizedPath(path, locale), alternates: { languages: languageAlternates(path) } })));
}
