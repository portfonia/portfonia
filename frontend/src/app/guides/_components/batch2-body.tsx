"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipContentProps } from "recharts";
import { Button } from "@/components/ui/button";
import { LanguageLinks } from "@/app/_components/language-links";
import { useLocale, useLocalizedHref } from "@/app/_components/locale-provider";
import { BENCHMARK_COLORS, PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import { formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import type { StressExample, StyleExample, TrackingExample } from "../_lib/example-batch2";

const UNAVAILABLE = "\u2014";
type CurveKey = "portfolio" | "spy" | "styleMix" | "balanced";

function seriesRow(payload: TooltipContentProps["payload"] | undefined, keys: readonly CurveKey[]): Record<string, unknown> | null {
  if (!payload) return null;
  for (const item of payload) {
    if (typeof item.dataKey !== "string" || !keys.includes(item.dataKey as CurveKey)) continue;
    const row: unknown = item.payload;
    if (typeof row === "object" && row !== null) return row as Record<string, unknown>;
  }
  return null;
}

function entryText(row: Record<string, unknown>, key: string, format: (value: number) => string): string {
  const value = row[key];
  return typeof value === "number" && Number.isFinite(value) ? format(value) : UNAVAILABLE;
}

function BatchTooltip({ active, label, payload, firstKey, secondKey, firstName, secondName, firstStroke, secondStroke, formatValue, formatDate }: {
  active?: boolean;
  label?: unknown;
  payload?: TooltipContentProps["payload"];
  firstKey: CurveKey;
  secondKey: CurveKey;
  firstName: string;
  secondName: string;
  firstStroke: string;
  secondStroke: string;
  formatValue: (value: number) => string;
  formatDate: (stamp: number) => string;
}) {
  const series = [
    { key: firstKey, name: firstName, color: firstStroke },
    { key: secondKey, name: secondName, color: secondStroke },
  ];
  const row = seriesRow(payload, [firstKey, secondKey]);
  const parsed = typeof label === "number" || typeof label === "string" ? Number(label) : Number.NaN;
  const stamp = Number.isFinite(parsed) ? parsed : typeof row?.time === "number" ? row.time : Number.NaN;
  if (!active || !row || !Number.isFinite(stamp)) return null;
  return <div className="public-curve-tooltip max-w-full break-words rounded-lg border px-3 py-2 text-sm shadow-lg" style={{ background: "var(--card)", color: "var(--foreground)", borderColor: "var(--border)" }}>
    <p className="mb-1 font-medium tabular-nums">{formatDate(stamp)}</p>
    <ul className="flex flex-col gap-1">
      {series.map((item) => <li key={item.key} className="flex items-center justify-between gap-4">
        <span className="flex min-w-0 items-center gap-2">
          <span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ backgroundColor: item.color }} />
          <span className="break-words">{item.name}</span>
        </span>
        <span className="shrink-0 tabular-nums">{entryText(row, item.key, formatValue)}</span>
      </li>)}
    </ul>
  </div>;
}

type Props = { kind: "stress"; example: StressExample } | { kind: "style"; example: StyleExample } | { kind: "tracking"; example: TrackingExample };
const symbols = ["IWF", "IWD", "IWM", "EFA", "EEM", "2800.HK", "510300.SS", "AGG", "TLT", "GLD", "DBC", "VNQ", "BIL"];
const scenarios = ["dotcom_2000", "gfc_2008", "covid_2020", "rates_2022", "tariffs_2025"];
const tableClass = "w-full table-fixed border-collapse text-xs sm:text-sm";
const cellClass = "min-w-0 break-words border-b border-foreground/10 px-1 py-3 text-left align-top";

export function Batch2Body(props: Props & { figureOnly?: boolean }) {
  const { kind, example, figureOnly = false } = props;
  const t = useTranslations("guides");
  const stress = useTranslations("jade.stress");
  const style = useTranslations("jade.style");
  const tracking = useTranslations("jade.tracking");
  const asset = useTranslations("portfolio.assetClasses");
  const { locale } = useLocale();
  const href = useLocalizedHref();
  const percent = new Intl.NumberFormat(locale, { style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const whole = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 });
  const oneDecimal = new Intl.NumberFormat(locale, { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const number = new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "always" });
  const integer = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 });
  const money = new Intl.NumberFormat(locale, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const date = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" });
  const dateString = (d: string) => date.format(new Date(`${d}T00:00:00Z`));
  const portfolio = t("common.portfolio", { spyStart: whole.format(.6), aggStart: whole.format(.4) });
  const display = (value: string) => t("common.figureValue", { value });
  const values: Record<string, string> = { portfolio, exampleLabel: t("common.exampleLabel") };
  if (kind === "stress") {
    Object.assign(values, {
      start: dateString(example.start), end: dateString(example.end), peak: dateString(example.peak), trough: dateString(example.trough),
      shock: percent.format(example.shock), spyShock: percent.format(example.spyShock), amount: money.format(example.amount),
      drawdown: percent.format(example.portfolioMetrics.drawdown), spyDrawdown: percent.format(example.spyMetrics.drawdown),
      endReturn: percent.format(example.points[example.points.length - 1].portfolio), spyEndReturn: percent.format(example.points[example.points.length - 1].spy),
      equityContribution: number.format(example.contributions[0].value * 100), bondContribution: number.format(example.contributions[1].value * 100),
      peakWeight: oneDecimal.format(example.peakSpyWeight),
    });
  } else if (kind === "style") {
    Object.assign(values, {
      start: dateString(example.start), end: dateString(example.end), samples: integer.format(example.sampleCount), levels: integer.format(example.points.length),
      equityStyles: formatTickPct(example.portfolio.weights[0] + example.portfolio.weights[1]),
      bondStyles: formatTickPct(example.portfolio.weights[7] + example.portfolio.weights[8]), bilWeight: formatTickPct(example.portfolio.displayWeights[12]),
      zeroWeight: formatTickPct(example.spy.displayWeights[7]), r2: example.portfolio.rSquaredText, spyR2: example.spy.rSquaredText,
      residualVol: formatTickPct(example.portfolio.residualVol), spyResidualVol: formatTickPct(example.spy.residualVol),
      cumulative: percent.format(example.points[example.points.length - 1].portfolio), mixCumulative: percent.format(example.points[example.points.length - 1].styleMix),
    });
  } else {
    const points = example.points;
    Object.assign(values, {
      start: dateString(points[0].date), end: dateString(points[points.length - 1].date), oneStart: dateString(example.oneYear.dates[0]),
      balancedExcess: number.format(example.fiveYear.balanced.excessReturn * 100), spyExcess: number.format(example.fiveYear.spy.excessReturn * 100),
      oneSamples: integer.format(example.oneYear.sampleCount), fiveSamples: integer.format(example.fiveYear.sampleCount), endWeight: oneDecimal.format(example.endSpyWeight),
      oneBalancedTe: percent.format(example.oneYear.balanced.trackingError), fiveBalancedTe: percent.format(example.fiveYear.balanced.trackingError),
      oneSpyTe: percent.format(example.oneYear.spy.trackingError), fiveSpyTe: percent.format(example.fiveYear.spy.trackingError),
    });
    for (const key of ["balanced", "spy"] as const) {
      const low = points.reduce((a, b) => b[key] < a[key] ? b : a), high = points.reduce((a, b) => b[key] > a[key] ? b : a);
      values[`${key}Min`] = number.format(low[key] * 100); values[`${key}Max`] = number.format(high[key] * 100);
      values[`${key}MinDate`] = dateString(low.date); values[`${key}MaxDate`] = dateString(high.date);
    }
  }

  function curve() {
    const isStress = props.kind === "stress", isStyle = props.kind === "style";
    const data: { date: string; time: number; portfolio?: number; spy?: number; styleMix?: number; balanced?: number }[] = props.example.points;
    const firstKey = props.kind === "tracking" ? "balanced" : "portfolio";
    const secondKey = isStyle ? "styleMix" : "spy";
    const firstName = props.kind === "tracking" ? t("tracking.balancedLegend") : portfolio;
    const secondName = isStyle ? style("legend.styleMix") : props.kind === "tracking" ? t("tracking.spyLegend") : "SPY";
    const firstStroke = PORTFOLIO_COLOR;
    const secondStroke = isStyle ? "var(--muted-foreground)" : BENCHMARK_COLORS.sp500;
    return <figure className="my-4 min-w-0">
      <div data-guide-chart data-first-stroke={firstStroke} data-second-stroke={secondStroke} className="h-60 w-full min-w-0" aria-label={t(`${kind}.title`)}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis type="number" dataKey="time" domain={["dataMin", "dataMax"]} tickFormatter={v => date.format(new Date(Number(v)))} minTickGap={24} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} />
            <YAxis width={48} tickFormatter={isStyle ? formatTickPct : v => percent.format(v)} tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} />
            <Tooltip isAnimationActive={false} cursor={{ stroke: "var(--foreground)" }} wrapperStyle={{ maxWidth: "100%" }} content={(props) => <BatchTooltip active={props.active} label={props.label} payload={props.payload} firstKey={firstKey} secondKey={secondKey} firstName={firstName} secondName={secondName} firstStroke={firstStroke} secondStroke={secondStroke} formatValue={isStyle ? formatTickPct : (value) => percent.format(value)} formatDate={(stamp) => date.format(new Date(stamp))} />} />
            <Line dataKey={firstKey} name={firstName} stroke={firstStroke} strokeWidth={2.5} dot={false} isAnimationActive={false} />
            <Line dataKey={secondKey} name={secondName} stroke={secondStroke} strokeWidth={1.5} dot={false} isAnimationActive={false} />
            {isStress && props.kind === "stress" && <>
              <ReferenceLine x={Date.parse(props.example.peak)} stroke="var(--muted-foreground)" strokeDasharray="6 3" />
              <ReferenceLine x={Date.parse(props.example.trough)} stroke="var(--muted-foreground)" strokeDasharray="2 3" />
            </>}
            {props.kind === "tracking" && <ReferenceLine y={0} stroke="var(--muted-foreground)" strokeDasharray="3 3" />}
            <Legend wrapperStyle={{ fontSize: 10 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <figcaption className="mt-3 break-words text-xs leading-relaxed text-foreground/70">{t(`${kind}.curveCaption`, values)}</figcaption>
    </figure>;
  }
  function stressTable() {
    if (props.kind !== "stress") return null;
    const e = props.example;
    return <table className={tableClass}>
      <caption className="mb-3 text-left leading-relaxed">{t("stress.tableCaption", values)}</caption>
      <thead><tr><th className={cellClass}>{t("common.metric")}</th><th className={cellClass}>{portfolio}</th><th className={cellClass}>SPY</th></tr></thead>
      <tbody>
        <tr><th scope="row" className={cellClass}>{stress("rows.shock")}</th><td data-cell="shock-portfolio" className={cellClass}>{display(values.shock)}<p>{display(values.amount)}</p></td><td className={cellClass}>{display(values.spyShock)}</td></tr>
        <tr><th scope="row" className={cellClass}>{stress("rows.maxDrawdown")}</th>{[e.portfolioMetrics, e.spyMetrics].map((m, i) => <td key={i} className={cellClass}>{display(percent.format(m.drawdown))}<p>{t("common.dateRange", { start: dateString(m.peakDate), end: dateString(m.troughDate) })}</p></td>)}</tr>
      </tbody>
    </table>;
  }
  function stressBars() {
    if (props.kind !== "stress") return null;
    // Production contributions arrive at six-decimal precision.
    const rows = props.example.contributions.map(c => ({ ...c, display: Math.round(c.value * 1e6) / 1e6 }));
    const max = Math.max(...rows.map(c => Math.abs(c.display)));
    return <figure className="my-4 min-w-0">
      <div className="flex min-w-0 flex-col gap-3" aria-label={stress("contributionsTitle")}>
        {rows.map(c => <div key={c.assetClass} data-contribution={c.assetClass} className="min-w-0">
          <div className="flex flex-wrap justify-between gap-2 text-xs"><span>{asset(c.assetClass)}</span><span>{display(number.format(c.display * 100))}</span></div>
          <div data-track className="relative h-2 w-full min-w-0 bg-muted"><span className="absolute inset-y-0 left-1/2 w-px bg-muted-foreground" /><span data-bar className="absolute inset-y-0" style={{ ...(c.display < 0 ? { right: "50%" } : { left: "50%" }), width: `${Math.abs(c.display) / max * 50}%`, background: c.display < 0 ? "var(--destructive)" : PORTFOLIO_COLOR }} /></div>
        </div>)}
      </div>
      <figcaption className="mt-3 break-words text-xs leading-relaxed text-foreground/70">{t("stress.barsCaption", values)}</figcaption>
    </figure>;
  }
  function contributionsTable() {
    if (props.kind !== "stress") return null;
    return <table className={tableClass}>
      <caption className="mb-3 text-left leading-relaxed">{t("stress.contributionCaption", values)}</caption>
      <thead><tr><th className={cellClass}>{stress("contributionsTitle")}</th><th className={cellClass}>{t("stress.contribution")}</th></tr></thead>
      <tbody>{props.example.contributions.map(c => <tr key={c.assetClass}><th scope="row" className={cellClass}>{asset(c.assetClass)}</th><td className={cellClass}>{display(number.format(c.value * 100))}</td></tr>)}
        <tr><th scope="row" className={cellClass}>{t("stress.total")}</th><td className={cellClass}>{display(values.shock)}</td></tr>
      </tbody>
    </table>;
  }
  function styleGrid() {
    if (props.kind !== "style") return null;
    const e = props.example;
    return <figure className="my-4 min-w-0">
      <table className={tableClass} aria-label={style("title")}>
        <caption className="mb-3 text-left leading-relaxed">{t("style.tableCaption", values)}</caption>
        <thead><tr><th className={cellClass}>{style("columns.style")}</th><th className={cellClass}>{portfolio}</th><th className={cellClass}>SPY</th></tr></thead>
        <tbody>{e.listedSymbols.map(symbol => {
          const j = symbols.indexOf(symbol), w = e.portfolio.displayWeights[j];
          return <tr key={symbol} data-style-row={symbol}>
            <th scope="row" className={cellClass}>{style(`labels.${symbol.replaceAll(".", "_")}`)} ({symbol})</th>
            <td className={cellClass}><span data-cell={`${symbol}-portfolio`}>{display(formatTickPct(w))}</span><div className="h-2 w-full min-w-0 bg-muted"><span data-style-bar className="block h-full" style={{ width: `${w * 100}%`, background: PORTFOLIO_COLOR }} /></div></td>
            <td data-cell={`${symbol}-spy`} className={cellClass}>{display(formatTickPct(e.spy.displayWeights[j]))}</td>
          </tr>;
        })}
          <tr><th scope="row" className={cellClass}>{style("rows.rSquared")}</th><td className={cellClass}>{display(values.r2)}</td><td className={cellClass}>{display(values.spyR2)}</td></tr>
          <tr><th scope="row" className={cellClass}>{style("rows.residualVol")}</th><td className={cellClass}>{display(values.residualVol)}</td><td className={cellClass}>{display(values.spyResidualVol)}</td></tr>
        </tbody>
      </table>
      <figcaption className="mt-3 break-words text-xs leading-relaxed text-foreground/70">{t("style.barsCaption", values)}</figcaption>
    </figure>;
  }
  function trackingTables() {
    if (props.kind !== "tracking") return null;
    const e = props.example;
    const rows = [["portfolio", "portfolioReturn"], ["reference", "referenceReturn"], ["excess", "excessReturn"], ["annualizedExcess", "annualizedExcess"], ["trackingError", "trackingError"]] as const;
    return <div className="mt-4 flex min-w-0 flex-col gap-6">{(["balanced", "spy"] as const).map(reference => <table key={reference} className={tableClass}>
      <caption className="mb-3 text-left leading-relaxed">{t("tracking.tableCaption", { ...values, reference: t(reference === "balanced" ? "tracking.balancedReference" : "tracking.spyReference", values) })}</caption>
      <thead><tr><th className={cellClass}>{t("common.metric")}</th><th className={cellClass}>{t("tracking.oneYear")}</th><th className={cellClass}>{t("tracking.fiveYear")}</th></tr></thead>
      <tbody>{rows.map(([label, field]) => <tr key={field}><th scope="row" className={cellClass}>{tracking(`rows.${label}`)}</th>{(["oneYear", "fiveYear"] as const).map(span => <td data-cell={`${reference}-${span}-${field}`} key={span} className={cellClass}>{display(percent.format(e[span][reference][field]))}</td>)}</tr>)}
        <tr><th scope="row" className={cellClass}>{t("tracking.samples")}</th><td className={cellClass}>{display(values.oneSamples)}</td><td className={cellClass}>{display(values.fiveSamples)}</td></tr>
      </tbody>
    </table>)}</div>;
  }
  if (figureOnly) return curve();
  const sections = t.raw(`${kind}.sections`) as Record<string, Record<string, string>>;
  return <main className="mx-auto w-full max-w-2xl px-6 py-12">
    <h1 className="font-serif text-3xl">{t(`${kind}.title`)}</h1>
    <p className="mt-6 break-words text-sm leading-relaxed text-foreground/80">{t(`${kind}.intro`, values)}</p>
    <div className="mt-10 flex min-w-0 flex-col gap-8">{Object.entries(sections).map(([key, section]) => <section key={key} className="min-w-0">
      <h2 className="font-serif text-lg">{t(`${kind}.sections.${key}.heading`)}</h2>
      {kind === "stress" && key === "s2" && <div className="mt-3 flex flex-wrap gap-1" role="group" aria-label={stress("scenarioLabel")}>
        {scenarios.map(id => <Button key={id} type="button" size="sm" aria-pressed={id === "tariffs_2025"} disabled={id !== "tariffs_2025"} variant={id === "tariffs_2025" ? "default" : "outline"}>{stress(`scenarios.${id}.name`)}</Button>)}
      </div>}
      <div className="mt-2 flex min-w-0 flex-col gap-3">{Object.keys(section).filter(k => k.startsWith("p")).map(k => <p className="break-words text-sm leading-relaxed text-foreground/70" key={k}>{t(`${kind}.sections.${key}.${k}`, values)}</p>)}</div>
      {kind === "style" && key === "s3" && <ul className="mt-3 list-disc space-y-2 pl-5 text-sm">{symbols.map(symbol => <li key={symbol} className="break-words">{style(`labels.${symbol.replaceAll(".", "_")}`)} ({symbol})</li>)}</ul>}
      {((kind === "stress" && key === "s2") || (kind === "style" && key === "s5") || (kind === "tracking" && key === "s4")) && curve()}
      {kind === "stress" && key === "s3" && stressTable()}
      {kind === "stress" && key === "s6" && stressBars()}
      {kind === "style" && key === "s4" && styleGrid()}
      {Object.keys(section).some(k => k.startsWith("r")) && <ol className="mt-3 list-decimal space-y-3 pl-5 text-sm leading-relaxed text-foreground/70">{Object.keys(section).filter(k => k.startsWith("r")).map(k => <li key={k} className="break-words">{t(`${kind}.sections.${key}.${k}`, values)}</li>)}</ol>}
      {kind === "stress" && key === "s6" && contributionsTable()}
      {kind === "tracking" && key === "s5" && trackingTables()}
    </section>)}</div>
    <nav className="mt-12 flex flex-wrap gap-4 border-t border-foreground/10 pt-6 text-sm text-foreground/60">
      <Link href={href("/pricing")} className="underline">{t("common.pricingLink")}</Link>
      <Link href={href("/guides")} className="underline">{t("common.guidesLink")}</Link>
    </nav>
    <LanguageLinks />
  </main>;
}
