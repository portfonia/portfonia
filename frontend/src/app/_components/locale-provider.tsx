"use client";

import { createContext, useContext, useEffect, useState } from "react";
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

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(DEFAULT_LOCALE);

  useEffect(() => {
    // One-time hydration-safe restore: render the default locale on both
    // server and first client paint (no mismatch), then swap to the stored
    // preference right after mount. A lazy useState initializer would read
    // localStorage during the client's first render too, which is exactly
    // what causes a hydration text mismatch against the server-rendered HTML.
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored === LEGACY_ZH_VALUE) {
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setLocaleState("zh-Hans");
      } else if (stored && isLocale(stored)) {
        setLocaleState(stored);
      }
    } catch {
      // Storage inaccessible (private browsing, blocked, quota) — fall
      // back to the default already set; nothing to restore.
    }
  }, []);

  useEffect(() => {
    // blacktomb42 review (PR #226): issue #209 requires html/lang to match
    // the selected locale on every route. AppShell's `lang` on its wrapper
    // div was never enough — screen readers and in-browser translate key
    // off the real `<html>` element, which layout.tsx must render
    // statically as "en" server-side (locale is client-only — see this
    // directory's README's "No URL-based locale routing" section). This
    // effect is the one place that keeps the real element in sync, since
    // LocaleProvider is the only place `locale` state changes (both the
    // storage restore above and setLocale below).
    document.documentElement.lang = locale;
  }, [locale]);

  function setLocale(next: Locale) {
    setLocaleState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Persistence best-effort only — the toggle still works for this
      // session even if it can't be saved.
    }
  }

  return (
    <LocaleContext.Provider value={{ locale, setLocale }}>
      <NextIntlClientProvider locale={locale} messages={catalogs[locale]}>
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
    boundary: t.raw("boundary"),
    faq: t.raw("faq"),
    status: t.raw("status"),
    footer: t.raw("footer"),
  };
}
