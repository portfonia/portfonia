"use client";

import { createContext, useContext, useEffect, useState } from "react";

import { homeMessages, type Locale } from "@/lib/i18n/home-messages";

const STORAGE_KEY = "portfonia:locale";

const LocaleContext = createContext<{
  locale: Locale;
  setLocale: (locale: Locale) => void;
} | null>(null);

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>("en");

  useEffect(() => {
    // One-time hydration-safe restore: render "en" on both server and first
    // client paint (no mismatch), then swap to the stored preference right
    // after mount. A lazy useState initializer would read localStorage
    // during the client's first render too, which is exactly what causes a
    // hydration text mismatch against the server-rendered "en" HTML.
    const stored = window.localStorage.getItem(STORAGE_KEY);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (stored === "en" || stored === "zh") setLocaleState(stored);
  }, []);

  function setLocale(next: Locale) {
    setLocaleState(next);
    window.localStorage.setItem(STORAGE_KEY, next);
  }

  return (
    <LocaleContext.Provider value={{ locale, setLocale }}>
      {children}
    </LocaleContext.Provider>
  );
}

export function useLocale() {
  const ctx = useContext(LocaleContext);
  if (!ctx) throw new Error("useLocale must be used within a LocaleProvider");
  return ctx;
}

export function useHomeMessages() {
  const { locale } = useLocale();
  return homeMessages[locale];
}
