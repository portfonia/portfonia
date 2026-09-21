"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";

import { GetStartedMenu } from "@/components/get-started-menu";
import { LocaleSwitcher } from "@/components/locale-switcher";

export function SiteHeader() {
  const pathname = usePathname();
  const isHome = pathname === "/";
  const tCommon = useTranslations("common");

  // One bar shape on every route (issue #207 R1): brand + language + menu.
  // The locale switcher used to be home-only because messages.ts had no zh
  // map — now that every route reads the same catalog (issue #209), it
  // shows everywhere. Issue #350 item 4: LocaleSwitcher replaces the plain
  // native <select> with the same Base UI Menu styling as GetStartedMenu.
  return (
    <header className="sticky top-4 z-20 flex justify-center px-4">
      <nav className="flex w-full max-w-4xl items-center justify-between gap-4 rounded-full border border-white/10 bg-card/70 px-5 py-3 backdrop-blur-md">
        <Link href={isHome ? "#top" : "/"} className="font-serif text-lg tracking-tight">
          {tCommon("brandName")}
        </Link>
        <div className="flex items-center gap-4">
          <LocaleSwitcher />
          <GetStartedMenu />
        </div>
      </nav>
    </header>
  );
}
