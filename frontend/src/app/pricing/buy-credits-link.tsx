"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";

export function BuyCreditsLink() {
  const t = useTranslations("legal.nav");
  return <div className="mx-auto max-w-2xl px-6 pb-10"><Button render={<Link href="/profile" />}>{t("buyCredits")}</Button></div>;
}
