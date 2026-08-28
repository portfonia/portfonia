"use client";

import { useTranslations } from "next-intl";

// Split out from page.tsx, same pattern as login-heading.tsx/signup-heading.tsx
// (issue #209): locale is only known client-side (no URL-based routing — see
// src/locales/README.md).
export function ForgotPasswordHeading() {
  const t = useTranslations("auth");
  return (
    <div className="text-center">
      <h1 className="font-serif text-3xl">{t("forgotPasswordHeading")}</h1>
      <p className="mt-2 text-sm text-foreground/60">{t("forgotPasswordSubtitle")}</p>
    </div>
  );
}
