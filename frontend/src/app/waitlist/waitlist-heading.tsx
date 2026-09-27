"use client";

import { useTranslations } from "next-intl";

export function WaitlistHeading() {
  const t = useTranslations("auth");
  return (
    <div className="text-center">
      <h1 className="font-serif text-3xl">{t("waitlistHeading")}</h1>
      <p className="mt-2 text-sm text-foreground/60">{t("waitlistSubtitle")}</p>
    </div>
  );
}
