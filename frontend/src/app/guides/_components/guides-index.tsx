"use client";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { LanguageLinks } from "@/app/_components/language-links";
import { useLocalizedHref } from "@/app/_components/locale-provider";

export function GuidesIndex() {
  const t = useTranslations("guides");
  const href = useLocalizedHref();
  return <main className="mx-auto w-full max-w-2xl px-6 py-12">
    <h1 className="font-serif text-3xl">{t("index.title")}</h1>
    <p className="mt-6 break-words text-sm leading-relaxed text-foreground/80">{t("index.intro", { exampleLabel: t("common.exampleLabel") })}</p>
    <div className="mt-8 flex min-w-0 flex-col gap-4">{(["replay", "var", "stress", "style", "tracking", "what-if"] as const).map(kind => <section key={kind} data-guide-card className="min-w-0 rounded-lg border border-foreground/10 p-4">
      <h2 className="break-words font-serif text-lg"><Link href={href(`/guides/jade-${kind}`)} className="underline">{t(`${kind === "what-if" ? "whatIf" : kind}.title`)}</Link></h2>
      <p className="mt-2 break-words text-sm leading-relaxed text-foreground/70">{t(`index.summaries.${kind === "what-if" ? "whatIf" : kind}`)}</p>
    </section>)}</div>
    <nav className="mt-12 border-t border-foreground/10 pt-6 text-sm"><Link className="underline" href={href("/pricing")}>{t("common.pricingLink")}</Link></nav>
    <LanguageLinks />
  </main>;
}
