"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";

import { GetStartedMenu } from "@/components/get-started-menu";
import { useLocale } from "@/app/_components/locale-provider";
import { LOCALES, type Locale } from "@/locales";

export function SiteHeader() {
  const pathname = usePathname();
  const isHome = pathname === "/";
  const tCommon = useTranslations("common");
  const tMenu = useTranslations("menu");
  const { locale, setLocale } = useLocale();

  // One bar shape on every route (issue #207 R1): brand + language + menu.
  // The locale switcher used to be home-only because messages.ts had no zh
  // map — now that every route reads the same catalog (issue #209), it
  // shows everywhere.
  return (
    <header className="sticky top-4 z-20 flex justify-center px-4">
      <nav className="flex w-full max-w-4xl items-center justify-between gap-4 rounded-full border border-white/10 bg-card/70 px-5 py-3 backdrop-blur-md">
        <Link href={isHome ? "#top" : "/"} className="font-serif text-lg tracking-tight">
          {tCommon("brandName")}
        </Link>
        <div className="flex items-center gap-4">
          <label className="sr-only" htmlFor="locale-select">
            {tMenu("language")}
          </label>
          <select
            id="locale-select"
            value={locale}
            onChange={(e) => setLocale(e.target.value as Locale)}
            className="rounded-md border border-white/10 bg-transparent px-2 py-1.5 text-sm text-foreground/80"
          >
            {LOCALES.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
          <GetStartedMenu />
        </div>
      </nav>
    </header>
  );
}
