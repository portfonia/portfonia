"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { GetStartedMenu } from "@/components/get-started-menu";
import { useHomeMessages, useLocale } from "@/app/_components/locale-provider";
import { locales, type Locale } from "@/lib/i18n/home-messages";
import { messages } from "@/lib/messages";

export function SiteHeader() {
  const pathname = usePathname();
  const isHome = pathname === "/";
  const t = useHomeMessages();
  const { locale, setLocale } = useLocale();

  // One bar shape on every route (issue #207 R1): brand + [locale] + menu.
  return (
    <header className="sticky top-4 z-20 flex justify-center px-4">
      <nav className="flex w-full max-w-4xl items-center justify-between gap-4 rounded-full border border-white/10 bg-card/70 px-5 py-3 backdrop-blur-md">
        <Link href={isHome ? "#top" : "/"} className="font-serif text-lg tracking-tight">
          {messages.common.brandName}
        </Link>
        <div className="flex items-center gap-4">
          {/* Language switcher stays home-only until messages.ts gains a zh
              map — same gate as AppShell's route-scoped lang attribute. */}
          {isHome && (
            <>
              <label className="sr-only" htmlFor="locale-select">
                {t.nav.language}
              </label>
              <select
                id="locale-select"
                value={locale}
                onChange={(e) => setLocale(e.target.value as Locale)}
                className="rounded-md border border-white/10 bg-transparent px-2 py-1.5 text-sm text-foreground/80"
              >
                {locales.map((l) => (
                  <option key={l.value} value={l.value}>
                    {l.label}
                  </option>
                ))}
              </select>
            </>
          )}
          <GetStartedMenu />
        </div>
      </nav>
    </header>
  );
}
