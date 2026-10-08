import type { Metadata } from "next";
import { catalogs, type Locale } from "@/locales";

export const NOINDEX_METADATA: Metadata = { robots: { index: false, follow: true } };

export const SITE_URL = "https://portfonia.com";
export const SEO_PAGES = {
  home: "/", about: "/about", pricing: "/pricing", waitlist: "/waitlist",
  privacy: "/privacy", terms: "/terms", refund: "/refund",
} as const;
export type SeoPageKey = keyof typeof SEO_PAGES;
// Shared segment-cache keys require dynamic pages, staleTimes.dynamic = 0, and no cacheComponents/PPR to avoid cross-locale reuse.
export const ROUTE_LOCALE_HEADER = "x-portfonia-locale";
export const PROTECTED_PATH_PREFIXES = ["/holdings", "/portfolio", "/profile", "/questionnaire", "/reports", "/welcome"];
export const HREFLANG: Record<Locale, string[]> = {
  en: ["en"], "zh-Hans": ["zh-Hans", "zh-CN", "zh-SG", "zh"], "zh-Hant": ["zh-Hant", "zh-TW", "zh-HK", "zh-MO"],
};
export const OG_LOCALE: Record<Locale, string> = { en: "en_US", "zh-Hans": "zh_CN", "zh-Hant": "zh_TW" };

export function localizedPath(path: string, locale: Locale): string {
  return locale === "en" ? path : `/${locale}${path === "/" ? "" : path}`;
}

export function splitLocalePrefix(pathname: string): { locale: Locale | null; path: string } {
  for (const locale of ["zh-Hans", "zh-Hant"] as const) {
    if (pathname === `/${locale}`) return { locale, path: "/" };
    if (pathname.startsWith(`/${locale}/`)) return { locale, path: pathname.slice(locale.length + 1) };
  }
  return { locale: null, path: pathname };
}

export function isSeoPath(path: string): boolean {
  return Object.values(SEO_PAGES).some((p) => p === path);
}

export function languageAlternates(path: string): Record<string, string> {
  const languages: Record<string, string> = {};
  for (const locale of Object.keys(HREFLANG) as Locale[]) {
    for (const tag of HREFLANG[locale]) languages[tag] = SITE_URL + localizedPath(path, locale);
  }
  languages["x-default"] = SITE_URL + path;
  return languages;
}

export function buildPageMetadata(page: SeoPageKey, locale: Locale): Metadata {
  const { title, description } = catalogs[locale].seo.pages[page];
  const canonical = SITE_URL + localizedPath(SEO_PAGES[page], locale);
  const image = `${SITE_URL}/og/${locale}`;
  return {
    title, description,
    alternates: { canonical, languages: languageAlternates(SEO_PAGES[page]) },
    openGraph: {
      type: "website", siteName: "Portfonia", url: canonical, title, description,
      locale: OG_LOCALE[locale],
      alternateLocale: (Object.keys(OG_LOCALE) as Locale[]).filter((l) => l !== locale).map((l) => OG_LOCALE[l]),
      images: [{ url: image, width: 1200, height: 630, alt: catalogs[locale].seo.ogImageAlt }],
    },
    twitter: { card: "summary_large_image", title, description, images: [image] },
  };
}
