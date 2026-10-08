"use client";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useLocalizedHref } from "../_components/locale-provider";
import type { Messages } from "@/locales";
export function AboutBody() {
  const t = useTranslations("about");
  const href = useLocalizedHref();
  const sections: Messages["about"]["sections"] = t.raw("sections");
  return <main className="mx-auto w-full max-w-2xl px-6 py-12">
    <h1 className="font-serif text-3xl">{t("title")}</h1>
    <p className="mt-6 text-sm leading-relaxed text-foreground/80">{t("intro")}</p>
    <div className="mt-10 flex flex-col gap-8">{sections.map((section) => <section key={section.heading}>
      <h2 className="font-serif text-lg">{section.heading}</h2>
      <div className="mt-2 flex flex-col gap-3">{section.body.map((paragraph) => <p key={paragraph} className="text-sm leading-relaxed text-foreground/70">{paragraph}</p>)}</div>
    </section>)}</div>
    <nav className="mt-12 flex flex-wrap gap-4 border-t border-white/10 pt-6 text-sm text-foreground/60">
      <Link href={href("/waitlist")} className="underline">{t("cta.waitlist")}</Link>
      <Link href={href("/pricing")} className="underline">{t("cta.pricing")}</Link>
    </nav>
  </main>;
}
