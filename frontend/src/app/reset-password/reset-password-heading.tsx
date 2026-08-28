"use client";

import { useTranslations } from "next-intl";

// Same split-out pattern as login-heading.tsx/signup-heading.tsx (issue #209).
export function ResetPasswordHeading() {
  const t = useTranslations("auth");
  return (
    <div className="text-center">
      <h1 className="font-serif text-3xl">{t("resetPasswordHeading")}</h1>
      <p className="mt-2 text-sm text-foreground/60">{t("resetPasswordSubtitle")}</p>
    </div>
  );
}
