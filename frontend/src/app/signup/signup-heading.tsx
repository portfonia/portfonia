"use client";

import { useTranslations } from "next-intl";

// Split out from page.tsx (issue #209) — see login-heading.tsx for why.
export function SignupHeading() {
  const t = useTranslations("auth");
  return (
    <div className="text-center">
      <h1 className="font-serif text-3xl">{t("signupHeading")}</h1>
      <p className="mt-2 text-sm text-foreground/60">{t("signupSubtitle")}</p>
    </div>
  );
}
