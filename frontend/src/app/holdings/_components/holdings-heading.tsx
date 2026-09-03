"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

// Split out from page.tsx (issue #209) — see login/login-heading.tsx for why.
export function HoldingsHeading() {
  const t = useTranslations("holdings");
  return (
    <header className="mb-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="font-heading text-2xl font-semibold">{t("pageTitle")}</h1>
        <Link href="/portfolio" className="text-sm text-muted-foreground underline underline-offset-4">
          {t("viewPortfolioLink")}
        </Link>
      </div>
      <p className="mt-1 text-sm text-muted-foreground">{t("pageSubtitle")}</p>
    </header>
  );
}
