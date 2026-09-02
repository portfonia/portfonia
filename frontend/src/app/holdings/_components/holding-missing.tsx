"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

export function HoldingMissing({ loadError }: { loadError: boolean }) {
  const t = useTranslations("holdings");
  return (
    <>
      <p className="text-sm text-destructive" role="alert">
        {loadError ? t("errorLoadFailed") : t("errorNotFound")}
      </p>
      <p className="mt-4">
        <Link href="/holdings/edit" className="text-sm underline underline-offset-4">
          {t("backToEdit")}
        </Link>
      </p>
    </>
  );
}
