"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { ReplayChart, ReplayLegend } from "@/app/guides/_components/replay-chart";
import { calculateExample } from "@/app/guides/_lib/example";
import { useLocale, useLocalizedHref } from "./locale-provider";

const JADE_EXAMPLE = calculateExample();
const TOOLS = [
  ["replay", "replay"],
  ["tailRisk", "var"],
  ["stress", "stress"],
  ["style", "style"],
  ["tracking", "tracking"],
] as const;

export function HomeJadeTools() {
  const t = useTranslations("home");
  const jade = useTranslations("jade");
  const guides = useTranslations("guides");
  const { locale } = useLocale();
  const href = useLocalizedHref();
  const percent = new Intl.NumberFormat(locale, {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const wholePercent = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 });
  const date = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" });
  const formatPercent = (v: number) => percent.format(v);
  const formatDate = (iso: string) => date.format(new Date(`${iso}T00:00:00Z`));
  const metrics = JADE_EXAMPLE.portfolio.metrics;
  const portfolio = guides("common.portfolio", {
    spyStart: wholePercent.format(JADE_EXAMPLE.constants.spyStart),
    aggStart: wholePercent.format(JADE_EXAMPLE.constants.aggStart),
  });
  const caption = guides("common.replayCaption", {
    portfolio,
    exampleLabel: guides("common.exampleLabel"),
    start: formatDate(JADE_EXAMPLE.start),
    end: formatDate(JADE_EXAMPLE.end),
    cumulative: formatPercent(metrics.cumulative),
    spyCumulative: formatPercent(JADE_EXAMPLE.spy.metrics.cumulative),
    drawdown: formatPercent(metrics.drawdown),
    peak: formatDate(metrics.peakDate),
    trough: formatDate(metrics.troughDate),
  });

  return (
    <section id="jade-tools" className="px-6 py-16 sm:py-20">
      <div className="mx-auto flex max-w-4xl flex-col gap-16">
        <div>
          <div className="mb-8 border-b border-white/10 pb-5">
            <h2 className="font-serif text-2xl sm:text-3xl">{t("jadeTools.heading")}</h2>
          </div>
          <p className="mb-6 max-w-2xl text-base leading-relaxed text-foreground/70">{t("jadeTools.body")}</p>
          <div className="min-w-0 rounded-xl border border-white/10 bg-card p-4" data-testid="home-jade-replay-chart">
            <figure>
              <div className="h-60 w-full min-w-0" aria-label={jade("replay.title")}>
                <ReplayChart
                  points={JADE_EXAMPLE.points}
                  peakDate={metrics.peakDate}
                  troughDate={metrics.troughDate}
                  portfolioName={portfolio}
                  formatPercent={formatPercent}
                  formatTick={v => date.format(new Date(Number(v)))}
                />
              </div>
              <ReplayLegend portfolioName={portfolio} />
              <figcaption className="mt-3 text-xs leading-relaxed text-foreground/70">{caption}</figcaption>
            </figure>
          </div>
        </div>
        <ul className="grid gap-4 sm:grid-cols-2">
          {TOOLS.map(([tool, route]) => (
            <li key={tool} className="rounded-lg border border-white/10 p-4">
              <Link href={href(`/guides/jade-${route}`)} className="font-semibold underline">{jade(`${tool}.title`)}</Link>
              <p className="mt-2 text-sm leading-relaxed text-foreground/70">{guides(`index.summaries.${route}`)}</p>
            </li>
          ))}
        </ul>
        <Link href={href("/guides")} className="text-sm underline">{jade("guideLinks.index")}</Link>
        <div className="rounded-2xl border border-white/10 bg-card p-7 sm:p-11">
          <h2 className="max-w-[18ch] font-serif text-2xl sm:text-3xl">{t("jadeTools.ctaHeading")}</h2>
          <p className="mt-4 max-w-xl text-base leading-relaxed text-foreground/70">{t("jadeTools.ctaBody")}</p>
          <Link href="/holdings" className="mt-8 inline-flex rounded-md bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground">
            {t("hero.ctaPrimary")}
          </Link>
        </div>
      </div>
    </section>
  );
}
