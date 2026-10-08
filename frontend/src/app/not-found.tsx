"use client";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useLocalizedHref } from "./_components/locale-provider";
export default function NotFound() {
  const t = useTranslations("notFound");
  const href = useLocalizedHref();
  return <main className="mx-auto w-full max-w-2xl px-6 py-12">
    <h1 className="font-serif text-3xl">{t("title")}</h1>
    <p className="mt-6 text-sm leading-relaxed text-foreground/70">{t("body")}</p>
    <Link href={href("/")} className="mt-8 inline-block text-sm underline">{t("homeLink")}</Link>
  </main>;
}
