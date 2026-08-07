"use client";

import { locales, type Locale } from "@/lib/i18n/home-messages";
import { useHomeMessages, useLocale } from "./locale-provider";

export function HomeNav() {
  const t = useHomeMessages();
  const { locale, setLocale } = useLocale();

  return (
    <div className="sticky top-4 z-20 flex justify-center px-4">
      <nav className="flex w-full max-w-4xl items-center justify-between gap-4 rounded-full border border-white/10 bg-card/70 px-5 py-3 backdrop-blur-md">
        <a href="#top" className="font-serif text-lg tracking-tight">
          Portfonia
        </a>
        <div className="flex items-center gap-5">
          <a
            href="#boundary"
            className="hidden text-sm text-foreground/70 hover:text-foreground sm:inline"
          >
            {t.nav.boundary}
          </a>
          <a
            href="#how"
            className="hidden text-sm text-foreground/70 hover:text-foreground sm:inline"
          >
            {t.nav.how}
          </a>
          <label className="sr-only" htmlFor="locale-select">
            Language
          </label>
          <select
            id="locale-select"
            value={locale}
            onChange={(e) => setLocale(e.target.value as Locale)}
            className="rounded-md border border-white/10 bg-transparent px-2 py-1.5 text-sm text-foreground/80"
          >
            {locales.map((l) => (
              <option key={l.value} value={l.value} className="text-black">
                {l.label}
              </option>
            ))}
          </select>
          <a
            href="/holdings"
            className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground"
          >
            {t.nav.cta}
          </a>
        </div>
      </nav>
    </div>
  );
}
