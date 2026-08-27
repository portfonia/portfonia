"use client";

import { useTranslations } from "next-intl";

// Split out from page.tsx (issue #209) — see login/login-heading.tsx for why.
export function HoldingsHeading() {
  const t = useTranslations("holdings");
  return (
    <header className="mb-8">
      <h1 className="font-heading text-2xl font-semibold">{t("pageTitle")}</h1>
      <p className="mt-1 text-sm text-muted-foreground">{t("pageSubtitle")}</p>
    </header>
  );
}
