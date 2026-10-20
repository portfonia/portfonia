"use client";

import { Fragment } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { LanguageLinks } from "@/app/_components/language-links";
import { useLocale, useLocalizedHref } from "@/app/_components/locale-provider";
import { INTL_LOCALES, formatShortDate, formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import type { WhatIfExample } from "../_lib/example-what-if";

const ruleKeys = ["none", "monthly", "quarterly"] as const;
const colors = { none: "#64748b", monthly: "#4e8b9d", quarterly: "#8b7bb8" };
const metricRows = [
  ["cumulative_return", "cumulative"],
  ["annualized_return", "annualized"],
  ["annualized_vol", "volatility"],
  ["max_drawdown", "drawdown"],
] as const;

export function WhatIfGuide({ example, figureOnly = false }: { example: WhatIfExample; figureOnly?: boolean }) {
  const t = useTranslations("guides.whatIf");
  const common = useTranslations("guides.common");
  const replay = useTranslations("jade.replay");
  const rules = useTranslations("jade.whatIf.rules");
  const { locale } = useLocale();
  const href = useLocalizedHref();
  const percent = new Intl.NumberFormat(INTL_LOCALES[locale], { style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const wholePercent = new Intl.NumberFormat(INTL_LOCALES[locale], { style: "percent", maximumFractionDigits: 0 });
  const integer = new Intl.NumberFormat(INTL_LOCALES[locale], { maximumFractionDigits: 0 });
  const date = new Intl.DateTimeFormat(INTL_LOCALES[locale], { dateStyle: "medium", timeZone: "UTC" });
  const values: Record<string, string> = {
    spyStart: wholePercent.format(example.constants.spyStart),
    aggStart: wholePercent.format(example.constants.aggStart),
    start: date.format(new Date(`${example.start}T00:00:00Z`)),
    end: date.format(new Date(`${example.end}T00:00:00Z`)),
    monthlyResets: integer.format(example.monthlyResets),
    quarterlyResets: integer.format(example.quarterlyResets),
    endSpyWeight: percent.format(example.endSpyWeight),
    endAggWeight: percent.format(example.endAggWeight),
    exampleLabel: common("exampleLabel"),
  };
  values.portfolio = common("portfolio", values);
  for (const rule of ruleKeys) {
    for (const [, metric] of metricRows) values[`${rule}_${metric}`] = percent.format(example[rule][metric]);
  }
  const cell = "break-words border-b border-foreground/10 px-1 py-3 text-left align-top font-normal";

  function figures() {
    return <>
      <figure className="my-4 min-w-0">
        <div data-guide-chart className="h-60 w-full min-w-0">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={example.points} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} strokeDasharray="3 3" />
              <XAxis dataKey="time" type="number" domain={["dataMin", "dataMax"]} tickFormatter={value => formatShortDate(new Date(Number(value)).toISOString().slice(0, 10), locale)} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} />
              <YAxis width={52} tickFormatter={formatTickPct} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} />
              <Tooltip isAnimationActive={false} labelFormatter={value => date.format(new Date(Number(value)))} formatter={value => percent.format(Number(value))} wrapperStyle={{ maxWidth: "100%" }} />
              <Legend wrapperStyle={{ fontSize: 10 }} />
              {ruleKeys.map(rule => <Line key={rule} dataKey={rule} name={rules(rule)} stroke={colors[rule]} strokeWidth={2} dot={false} isAnimationActive={false} />)}
            </LineChart>
          </ResponsiveContainer>
        </div>
        <figcaption className="mt-3 break-words text-xs leading-relaxed text-foreground/70">{t("chartCaption", values)}</figcaption>
      </figure>
      <div className="max-w-full overflow-x-auto">
        <table className="w-full table-fixed border-collapse text-xs sm:text-sm">
          <caption className="mb-3 text-left leading-relaxed">{t("tableCaption", values)}</caption>
          <thead><tr><th scope="col" className={cell}>{common("metric")}</th>{ruleKeys.map(rule => <th scope="col" key={rule} className={cell}>{rules(rule)}</th>)}</tr></thead>
          <tbody>{metricRows.map(([label, metric]) => <tr key={metric}>
            <th scope="row" className={cell}>{replay(`metrics.${label}`)}</th>
            {ruleKeys.map(rule => <td key={rule} className={cell}>{percent.format(example[rule][metric])}</td>)}
          </tr>)}</tbody>
        </table>
      </div>
    </>;
  }

  if (figureOnly) return figures();
  const sections = t.raw("sections") as Record<string, Record<string, string>>;
  return <main className="mx-auto w-full max-w-2xl px-6 py-12">
    <h1 className="font-serif text-3xl">{t("title")}</h1>
    <p className="mt-6 break-words text-sm leading-relaxed text-foreground/80">{t("intro", values)}</p>
    <div className="mt-10 flex min-w-0 flex-col gap-8">{Object.entries(sections).map(([key, section]) => <section key={key} className="min-w-0">
      <h2 className="font-serif text-lg">{t(`sections.${key}.heading`)}</h2>
      <div className="mt-2 flex min-w-0 flex-col gap-3">{Object.keys(section).filter(paragraph => paragraph.startsWith("p")).map(paragraph => <Fragment key={paragraph}>
        <p className="break-words text-sm leading-relaxed text-foreground/70">{t(`sections.${key}.${paragraph}`, values)}</p>
        {key === "s5" && paragraph === "p2" && figures()}
      </Fragment>)}</div>
    </section>)}</div>
    <nav className="mt-12 flex flex-wrap gap-4 border-t border-foreground/10 pt-6 text-sm text-foreground/60">
      <Link className="underline" href={href("/pricing")}>{common("pricingLink")}</Link>
      <Link className="underline" href={href("/guides")}>{common("guidesLink")}</Link>
    </nav>
    <LanguageLinks />
  </main>;
}
