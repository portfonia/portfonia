"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { isSeoPath, localizedPath, splitLocalePrefix } from "@/lib/seo";
import { NextIntlClientProvider, useTranslations } from "next-intl";

import { catalogs, DEFAULT_LOCALE, isLocale, type Locale, type Messages } from "@/locales";

const STORAGE_KEY = "portfonia:locale";
// Pre-issue-#209 stored value (the old `home-messages.ts` Locale union was
// "en" | "zh"). Migrate transparently so a browser that already stored "zh"
// keeps resolving to Simplified Chinese instead of silently falling back to
// English once "zh" stops being a valid Locale value.
const LEGACY_ZH_VALUE = "zh";

const LocaleContext = createContext<{
  locale: Locale;
  setLocale: (locale: Locale) => void;
} | null>(null);

export function LocaleProvider({ children, routeLocale }: { children: React.ReactNode; routeLocale: Locale | null }) {
  const [locale, setLocaleState] = useState<Locale>(routeLocale ?? DEFAULT_LOCALE);
  const router = useRouter();
  const pathname = usePathname();
  const previousPathname = useRef(pathname);

  useEffect(() => {
    // URL locales match SSR; unprefixed routes restore storage after hydration.
    try {
      if (routeLocale) {
        window.localStorage.setItem(STORAGE_KEY, routeLocale);
        return;
      }
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored === LEGACY_ZH_VALUE) {
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setLocaleState("zh-Hans");
        // blacktomb42 round-2 review (PR #226, non-blocking): rewrite the
        // stored value too, not just the in-memory state — otherwise every
        // future page load re-interprets the same stale "zh" instead of the
        // migration actually completing once.
        window.localStorage.setItem(STORAGE_KEY, "zh-Hans");
      } else if (stored && isLocale(stored)) {
        setLocaleState(stored);
      }
    } catch {
      // Storage inaccessible (private browsing, blocked, quota) — fall
      // back to the default already set; nothing to restore.
    }
  }, [routeLocale]);

  useEffect(() => {
    // Keep the real document language aligned with both URL and stored preferences.
    document.documentElement.lang = locale;
  }, [locale]);

  useEffect(() => {
    function syncUrlLocale() {
      const { locale: prefix, path } = splitLocalePrefix(window.location.pathname);
      if (isSeoPath(path)) setLocaleState(prefix ?? DEFAULT_LOCALE);
    }
    // The first mount keeps the existing unprefixed-page storage restore.
    if (previousPathname.current !== pathname) {
      previousPathname.current = pathname;
      syncUrlLocale();
    }
    window.addEventListener("popstate", syncUrlLocale);
    return () => window.removeEventListener("popstate", syncUrlLocale);
  }, [pathname]);

  function setLocale(next: Locale) {
    setLocaleState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Persistence best-effort only — the toggle still works for this
      // session even if it can't be saved.
    }
    const { path } = splitLocalePrefix(window.location.pathname);
    if (isSeoPath(path)) router.push(localizedPath(path, next) + window.location.search + window.location.hash);
  }

  return (
    <LocaleContext.Provider value={{ locale, setLocale }}>
      {/* Project-wide ET default; also silences next-intl's ENVIRONMENT_FALLBACK during prerender. */}
      <NextIntlClientProvider locale={locale} messages={catalogs[locale]} timeZone="America/New_York">
        {children}
      </NextIntlClientProvider>
    </LocaleContext.Provider>
  );
}

export function useLocale() {
  const ctx = useContext(LocaleContext);
  if (!ctx) throw new Error("useLocale must be used within a LocaleProvider");
  return ctx;
}

export function useLocalizedHref() {
  const { locale } = useLocale();
  return (path: string) => isSeoPath(path) ? localizedPath(path, locale) : path;
}

// Convenience wrapper for home-sections.tsx (issue #209): the home catalog
// namespace has no ICU interpolation needs (no plurals/placeholders — just
// static strings, arrays, and objects for the marketing page), so t.raw()
// per top-level key reproduces the plain-object shape the old
// `home-messages.ts` export used to have. That keeps home-sections.tsx's
// existing object-access code (`t.hero.eyebrow`, `t.preview.holdingsRows.
// map(...)`) working unchanged against the new catalog.
export function useHomeMessages(): Messages["home"] {
  const t = useTranslations("home");
  // t.raw() is typed `any` (next-intl bypasses ICU processing entirely for
  // it), so without this return type annotation every array in the result
  // would need its .map() callbacks explicitly typed in home-sections.tsx —
  // the annotation restores real types from the catalog's own shape instead.
  return {
    hero: t.raw("hero"),
    how: t.raw("how"),
    preview: t.raw("preview"),
    audience: t.raw("audience"),
    boundary: t.raw("boundary"),
    faq: t.raw("faq"),
    status: t.raw("status"),
    footer: t.raw("footer"),
    productPreviews: t.raw("productPreviews"),
  };
}

// Same rationale as useHomeMessages above: t.raw() per top-level key restores
// real types instead of next-intl's untyped t.raw() return. The legal document
// trees contain {merchantOfRecord}/{resellerNotice} placeholders that
// LegalDocument fills itself; they must stay on t.raw() and never go through
// t(), which would parse them as ICU arguments.
export function useLegalMessages(): Messages["legal"] {
  const t = useTranslations("legal");
  return {
    nav: t.raw("nav"),
    resellerNotice: t.raw("resellerNotice"),
    terms: t.raw("terms"),
    privacy: t.raw("privacy"),
    pricing: t.raw("pricing"),
    refund: t.raw("refund"),
  };
}
