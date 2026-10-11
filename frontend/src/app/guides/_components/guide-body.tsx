"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, XAxis, YAxis } from "recharts";
import { LanguageLinks } from "@/app/_components/language-links";
import { useLocale, useLocalizedHref } from "@/app/_components/locale-provider";
import type { Example } from "../_lib/example";
import { PORTFOLIO_COLOR, BENCHMARK_COLORS } from "@/app/portfolio/performance/_components/performance-colors";
import type { Messages } from "@/locales";

export function GuideBody({ kind, example }: {
  kind: "replay" | "var";
  example: Example;
}) {
  const t = useTranslations("guides");
  const replay = useTranslations("jade.replay");
  const tail = useTranslations("jade.tailRisk");
  const { locale } = useLocale();
  const href = useLocalizedHref();
  const percent = new Intl.NumberFormat(locale, {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
  const wholePercent = new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits: 0
  });
  const integer = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 });
  const amount = new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0
  });
  const date = new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeZone: "UTC"
  });
  const month = new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "long",
    timeZone: "UTC"
  });
  const formatPercent = (v: number) => percent.format(v);
  const formatInteger = (v: number) => integer.format(v);
  const formatDate = (v: string) => date.format(new Date(`${v}T00:00:00Z`));
  const portfolioMetrics = example.portfolio.metrics;
  const spyMetrics = example.spy.metrics;
  const portfolioTail = example.portfolio.tail;
  const values = {
    exampleLabel: t("common.exampleLabel"),
    spyStart: wholePercent.format(example.constants.spyStart),
    aggStart: wholePercent.format(example.constants.aggStart),
    start: formatDate(example.start),
    end: formatDate(example.end),
    cumulative: formatPercent(portfolioMetrics.cumulative),
    spyCumulative: formatPercent(spyMetrics.cumulative),
    annualized: formatPercent(portfolioMetrics.annualized),
    spyAnnualized: formatPercent(spyMetrics.annualized),
    volatility: formatPercent(portfolioMetrics.volatility),
    spyVolatility: formatPercent(spyMetrics.volatility),
    drawdown: formatPercent(portfolioMetrics.drawdown),
    spyDrawdown: formatPercent(spyMetrics.drawdown),
    peak: formatDate(portfolioMetrics.peakDate),
    trough: formatDate(portfolioMetrics.troughDate),
    worstDay: formatPercent(portfolioMetrics.worstDay),
    spyWorstDay: formatPercent(spyMetrics.worstDay),
    worstDate: formatDate(portfolioMetrics.worstDayDate),
    worstMonth: formatPercent(portfolioMetrics.worstMonth),
    spyWorstMonth: formatPercent(spyMetrics.worstMonth),
    worstMonthLabel: month.format(new Date(`${portfolioMetrics.worstMonthLabel}-01T00:00:00Z`)),
    endSpyWeight: formatPercent(example.endSpyWeight),
    monthStart: formatDate(example.oneMonthStart),
    monthCumulative: formatPercent(example.oneMonth.cumulative),
    monthAnnualized: formatPercent(example.oneMonth.annualized),
    dailyCount: formatInteger(portfolioTail.dailyCount),
    dailyK95: formatInteger(portfolioTail.levels[0].daily.k),
    dailyK99: formatInteger(portfolioTail.levels[1].daily.k),
    monthCount: formatInteger(portfolioTail.monthCount),
    independentMonths: formatInteger(portfolioTail.independentMonths),
    dayVar: formatPercent(portfolioTail.levels[0].daily.var),
    dayCvar: formatPercent(portfolioTail.levels[0].daily.cvar),
    dayVar99: formatPercent(portfolioTail.levels[1].daily.var),
    normal95: formatPercent(portfolioTail.levels[0].normalDaily.var),
    normal99: formatPercent(portfolioTail.levels[1].normalDaily.var),
    fatCount: formatInteger(portfolioTail.fatCount),
    normalExpected: new Intl.NumberFormat(locale, {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1
    }).format(portfolioTail.normalExpected),
    worstLoss: formatPercent(-portfolioMetrics.worstDay),
    referenceDay: formatPercent(example.reference[0].daily),
  };
  const portfolio = t("common.portfolio", values);
  const all = {
    ...values,
    portfolio
  };
  const tableClass = "w-full table-fixed border-collapse text-xs sm:text-sm";
  const cellClass = "break-words border-b border-foreground/10 px-1 py-3 text-left align-top";
  const caption = t("common.window", all);
  const label = (series: string, confidence: number, metric: string) => t("common.tailRow", {
    series,
    confidence: formatPercent(confidence / 100),
    metric
  });
  const metricRows = [
    ["cumulative_return", "cumulative"],
    ["annualized_return", "annualized"],
    ["annualized_vol", "volatility"],
    ["max_drawdown", "drawdown"],
    ["worst_day", "worstDay"],
    ["worst_month", "worstMonth"],
  ] as const;

  function replayTable() {
    return <table className={tableClass}>
      <caption className="mb-3 text-left leading-relaxed">{caption}</caption>
      <thead>
        <tr>
          <th className={cellClass}>{t("common.metric")}</th>
          <th className={cellClass}>{portfolio}</th>
          <th className={cellClass}>SPY</th>
        </tr>
      </thead>
      <tbody>
        {metricRows.map(([key, field]) => <tr key={key}>
          <th scope="row" className={cellClass}>{replay(`metrics.${key}`)}</th>
          {[portfolioMetrics, spyMetrics].map((m, i) => <td className={cellClass} key={i}>
            {formatPercent(m[field])}
            {field === 'drawdown' && <p>
              {t("common.dateRange", {
                start: formatDate(m.peakDate),
                end: formatDate(m.troughDate)
              })}
            </p>}
            {field === 'worstDay' && <p>{formatDate(m.worstDayDate)}</p>}
            {field === 'worstMonth' && <p>{month.format(new Date(`${m.worstMonthLabel}-01T00:00:00Z`))}</p>}
          </td>)}
        </tr>)}
      </tbody>
    </table>;
  }
  type TailLevel = Example["portfolio"]["tail"]["levels"][number];

  function riskRow(seriesIndex: number, level: TailLevel, metric: "var" | "cvar") {
    const seriesKey = seriesIndex === 0 ? "portfolio" : "spy";
    const seriesName = seriesIndex === 0 ? portfolio : "SPY";
    return (
      <tr key={`${seriesKey}-${level.level}-${metric}`}>
        <th scope="row" className={cellClass}>{label(seriesName, level.level, tail(`rows.${metric}`))}</th>
        {(["daily", "monthly"] as const).map(horizon => (
          <td key={horizon} className={cellClass} data-cell={`${seriesKey}-${level.level}-${horizon}-${metric}`}>
            {formatPercent(level[horizon][metric])}
            {seriesIndex === 0 && <p>{amount.format(level[horizon][metric] * example.constants.baseAmount)}</p>}
          </td>
        ))}
      </tr>
    );
  }

  function riskRows() {
    return [example.portfolio, example.spy].flatMap((series, index) => {
      return series.tail.levels.flatMap(level => {
        return (["var", "cvar"] as const).map(metric => riskRow(index, level, metric));
      });
    });
  }

  function riskTable() {
    return <table className={tableClass}>
      <caption className="mb-3 text-left leading-relaxed">{caption}</caption>
      <thead>
        <tr>
          <th className={cellClass}>{t("common.metric")}</th>
          <th className={cellClass}>{t("common.day", values)}</th>
          <th className={cellClass}>{t("common.month", values)}</th>
        </tr>
      </thead>
      <tbody>
        {riskRows()}
        <tr>
          <th scope="row" className={cellClass}>{tail("rows.reference")}</th>
          {(['daily', 'monthly'] as const).map(horizon => <td key={horizon} className={cellClass}>
            {example.reference.map(r => <p key={r.level}>
              {t("common.referenceValue", {
                confidence: formatPercent(r.level / 100),
                value: formatPercent(r[horizon])
              })}
            </p>)}
          </td>)}
        </tr>
      </tbody>
    </table>;
  }

  function normalTable() {
    return <table className={tableClass}>
      <caption className="mb-3 text-left leading-relaxed">{t("common.normalCaption", all)}</caption>
      <thead>
        <tr>
          <th className={cellClass}>{t("common.metric")}</th>
          <th className={cellClass}>{t("common.historical")}</th>
          <th className={cellClass}>{t("common.normal")}</th>
        </tr>
      </thead>
      <tbody>
        {portfolioTail.levels.flatMap(level => (['var', 'cvar'] as const).map(metric => <tr key={`${level.level}-${metric}`}>
          <th scope="row" className={cellClass}>{label(portfolio, level.level, tail(`rows.${metric}`))}</th>
          <td className={cellClass}>{formatPercent(level.daily[metric])}</td>
          <td className={cellClass}>{formatPercent(level.normalDaily[metric])}</td>
        </tr>))}
      </tbody>
    </table>;
  }

  function histogramCounts() {
    const separator = {
      en: "; ",
      es: "; ",
      "zh-Hans": "\uFF1B",
      "zh-Hant": "\uFF1B"
    }[locale];
    const bins = example.histogram.map(bin => t("common.histogramBin", {
      lower: formatPercent(bin.lower),
      upper: formatPercent(bin.lower + 0.005),
      count: formatInteger(bin.count),
    }));
    return bins.join(separator);
  }

  function figure() {
    const histogramData = example.histogram.map(bin => ({
      ...bin,
      midpoint: bin.lower + 0.0025,
    }));
    return <figure className="my-4 min-w-0">
      <div data-guide-chart aria-label={t(`${kind}.sections.s2.heading`)} className="h-60 w-full min-w-0">
        {kind === 'replay' ? <ResponsiveContainer width="100%" height="100%">
          <LineChart data={example.points} margin={{
            top: 8,
            right: 8,
            bottom: 0,
            left: 0
          }}>
            <CartesianGrid stroke="var(--border)" vertical={false} />
            <XAxis type="number" dataKey="time" domain={['dataMin', 'dataMax']} tickFormatter={v => date.format(new Date(Number(v)))} minTickGap={36} tick={{
              fontSize: 10,
              fill: 'var(--muted-foreground)'
            }} />
            <YAxis width={48} tickFormatter={formatPercent} tick={{
              fontSize: 10,
              fill: 'var(--muted-foreground)'
            }} />
            <Line name={portfolio} dataKey="portfolio" stroke={PORTFOLIO_COLOR} dot={false} isAnimationActive={false} />
            <Line name="SPY" dataKey="spy" stroke={BENCHMARK_COLORS.sp500} dot={false} isAnimationActive={false} />
            <ReferenceLine x={Date.parse(portfolioMetrics.peakDate)} stroke="var(--muted-foreground)" strokeDasharray="3 3" />
            <ReferenceLine x={Date.parse(portfolioMetrics.troughDate)} stroke="var(--muted-foreground)" strokeDasharray="3 3" />
            <Legend wrapperStyle={{ fontSize: 10 }} />
          </LineChart>
        </ResponsiveContainer> : <ResponsiveContainer width="100%" height="100%">
          <BarChart data={histogramData}>
            <XAxis type="number" dataKey="midpoint" domain={['dataMin', 'dataMax']} tickFormatter={formatPercent} tick={{
              fontSize: 10,
              fill: 'var(--muted-foreground)'
            }} />
            <YAxis width={30} tickFormatter={formatInteger} tick={{
              fontSize: 10,
              fill: 'var(--muted-foreground)'
            }} />
            <Bar dataKey="count" name={t("common.frequency")} fill="var(--muted-foreground)" isAnimationActive={false} />
            <ReferenceLine x={-portfolioTail.levels[0].daily.var} stroke={PORTFOLIO_COLOR} />
            <ReferenceLine x={-portfolioTail.levels[0].daily.cvar} stroke={PORTFOLIO_COLOR} strokeDasharray="6 3" />
            <ReferenceLine x={-example.reference[0].daily} stroke="var(--foreground)" strokeDasharray="2 3" />
          </BarChart>
        </ResponsiveContainer>}
      </div>
      {kind === "var" && <div className="mt-2 flex flex-wrap gap-3 text-xs">
        <span style={{ color: PORTFOLIO_COLOR }}>{tail("legend.var")}</span>
        <span style={{ color: PORTFOLIO_COLOR }}>{tail("legend.cvar")}</span>
        <span>{tail("legend.reference")}</span>
      </div>}
      <figcaption className="mt-3 text-xs leading-relaxed text-foreground/70">
        {t(kind === 'replay' ? "common.replayCaption" : "common.varCaption", all)}
        {kind === 'var' && <p className="mt-2">{t("common.histogramCounts", { bins: histogramCounts() })}</p>}
      </figcaption>
    </figure>;
  }

  function paragraphKeys(section: Record<string, string>) {
    return Object.keys(section).filter(key => key !== "heading");
  }
  const sections: Messages['guides']['replay']['sections'] | Messages['guides']['var']['sections'] = kind === 'replay' ? t.raw('replay.sections') : t.raw('var.sections');
  return <main className="mx-auto w-full max-w-2xl px-6 py-12">
    <h1 className="font-serif text-3xl">{t(`${kind}.title`)}</h1>
    <p className="mt-6 text-sm leading-relaxed text-foreground/80">{t(`${kind}.intro`, all)}</p>
    <div className="mt-10 flex min-w-0 flex-col gap-8">
      {Object.entries(sections).map(([key, section]) => <section key={key} className="min-w-0">
        <h2 className="font-serif text-lg">{t(`${kind}.sections.${key}.heading`)}</h2>
        <div className="mt-2 flex min-w-0 flex-col gap-3">{paragraphKeys(section).map(k => <p className="break-words text-sm leading-relaxed text-foreground/70" key={k}>{t(`${kind}.sections.${key}.${k}`, all)}</p>)}</div>
        {key === 's2' && figure()}
        {kind === 'replay' && key === 's2' && replayTable()}
        {kind === 'var' && key === 's3' && riskTable()}
        {kind === 'var' && key === 's8' && normalTable()}
      </section>)}
    </div>
    <nav className="mt-12 flex flex-wrap gap-4 border-t border-foreground/10 pt-6 text-sm text-foreground/60">
      <Link href={href('/pricing')} className="underline">{t('common.pricingLink')}</Link>
      <Link href={href(kind === 'replay' ? '/guides/jade-var' : '/guides/jade-replay')} className="underline">{t(kind === 'replay' ? 'common.varLink' : 'common.replayLink')}</Link>
    </nav>
    <LanguageLinks />
  </main>;
}
