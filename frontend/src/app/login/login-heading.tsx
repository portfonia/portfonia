"use client";

import { useTranslations } from "next-intl";

// Split out from page.tsx (issue #209): locale is only known client-side
// (see src/locales/README.md — no URL-based routing), so a Server Component
// page cannot render translated text itself. This is the smallest client
// boundary that lets the rest of the page (searchParams handling) stay a
// Server Component.
export function LoginHeading() {
  const t = useTranslations("auth");
  return (
    <div className="text-center">
      <h1 className="font-serif text-3xl">{t("loginHeading")}</h1>
      <p className="mt-2 text-sm text-foreground/60">{t("loginSubtitle")}</p>
    </div>
  );
}
