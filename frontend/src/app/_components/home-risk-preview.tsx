"use client";

import { useTranslations } from "next-intl";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipContentProps } from "recharts";

import { useLocale } from "@/app/_components/locale-provider";
import {
  BetaGauge,
  DEVIATION_COLORS,
  ExplanationCell,
  isoFromTimestamp,
  pct,
  RISK_COLORS,
  RiskChartTooltip,
  SegmentedBar,
  toTimestamp,
  type ChartSeries,
} from "@/app/portfolio/_components/risk-panel";
import { BENCHMARK_COLORS, PORTFOLIO_COLOR } from "@/app/portfolio/performance/_components/performance-colors";
import { formatShortDate, formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import { HOME_RISK_SAMPLE } from "./home-risk-sample";

export function HomeRiskPreview() {
  const t = useTranslations("portfolio.riskPanel");
  const names = useTranslations("portfolio.performance.benchmarkNames");
  const { locale } = useLocale();
  const data = HOME_RISK_SAMPLE;
  const betaValue = Number(data.beta.value);
  const betaText = betaValue.toFixed(2);
  const riskText = t(`riskLabels.${data.risk.label!}`);
  const delta = data.deviation.delta!;
  const deviationText = delta > 0 ? t("aggressiveDirection", { count: delta })
    : delta < 0 ? t("conservativeDirection", { count: Math.abs(delta) }) : t("match");
  const portfolioSeries = data.portfolio_vol.points.map((point) => ({ t: toTimestamp(point.date), vol: Number(point.vol) }));
  const benchmarkSeries = data.benchmark_vols.map((item) => ({
    code: item.code,
    name: names(item.code),
    color: BENCHMARK_COLORS[item.code],
    current: item.current,
    points: item.points.map((point) => ({ t: toTimestamp(point.date), vol: Number(point.vol) })),
  }));
  const chartSeries: ChartSeries[] = [
    { name: t("portfolio"), color: PORTFOLIO_COLOR, points: portfolioSeries },
    ...benchmarkSeries.map(({ name, color, points }) => ({ name, color, points })),
  ];
  const timestamps = [...new Set(chartSeries.flatMap((item) => item.points.map((point) => point.t)))].sort((a, b) => a - b);
  const ticks = timestamps.length <= 6 ? timestamps : Array.from({ length: 6 }, (_, index) => timestamps[Math.round(index * (timestamps.length - 1) / 5)]);
  const chartName = data.benchmark_vols.map((item) => names(item.code)).join(", ");

  return <>
    <div className="grid grid-cols-1 gap-4 min-[461px]:grid-cols-2 min-[721px]:grid-cols-3">
      <ExplanationCell name={t("beta")} value={betaText} subtitle={t("betaSubtitle")} explanation={t("betaExplanation")}>
        <BetaGauge value={betaValue} />
      </ExplanationCell>
      <ExplanationCell name={t("risk")} value={riskText} subtitle={t("riskSubtitle")} explanation={t("riskExplanation")}>
        <p className="mt-7 min-h-12 font-heading text-xl text-primary">{riskText}</p>
        <SegmentedBar segments={RISK_COLORS.map((color, index) => ({ color, label: t(`riskLabels.${(["within", "caution", "exceeds"] as const)[index]}`) }))} activeIndex={1} />
        <p className="mt-4 text-xs text-muted-foreground">{t("objectiveTier", { tier: t(`tiers.${data.portfolio_vol.tier!}`) })}</p>
      </ExplanationCell>
      <ExplanationCell name={t("deviation")} value={deviationText} subtitle={t("deviationSubtitle")} explanation={t("deviationExplanation")}>
        <p className="mt-7 min-h-12 font-heading text-xl text-primary">{deviationText}</p>
        <SegmentedBar segments={DEVIATION_COLORS.map((color) => ({ color }))} activeIndex={delta + 2} endLabels={[t("conservative"), t("aggressive")]} middleLabel={t("matchShort")} />
        <p className="mt-4 text-xs text-muted-foreground">{t("deviationSubline")}</p>
      </ExplanationCell>
    </div>
    <section className="min-w-0 border-t border-border pt-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="font-heading text-lg">{t("volatilityTitle")}</h3>
      </div>
      <div className="mt-4 flex flex-wrap gap-5 text-sm">
        <span><span aria-hidden="true" className="mr-2 inline-block size-2 rounded-full" style={{ backgroundColor: PORTFOLIO_COLOR }} />{t("portfolio")}: <strong>{pct(data.portfolio_vol.current)}</strong></span>
        {benchmarkSeries.map((item) => <span key={item.code}><span aria-hidden="true" className="mr-2 inline-block size-2 rounded-full" style={{ backgroundColor: item.color }} />{item.name}: <strong>{pct(item.current)}</strong></span>)}
      </div>
      <div role="img" aria-label={t("chartDescription", { benchmark: chartName })} className="mt-5 h-56 w-full min-w-0">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis type="number" dataKey="t" scale="time" domain={["dataMin", "dataMax"]} ticks={ticks} minTickGap={24} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} tickLine={false} axisLine={false} tickFormatter={(value: number) => formatShortDate(isoFromTimestamp(value), locale)} />
            <YAxis tickFormatter={formatTickPct} domain={[0, "auto"]} width={48} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} tickLine={false} axisLine={false} />
            <Tooltip content={(props: TooltipContentProps) => <RiskChartTooltip {...props} series={chartSeries} />} />
            <Line data={portfolioSeries} dataKey="vol" type="monotone" stroke={PORTFOLIO_COLOR} strokeWidth={2.5} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />
            {benchmarkSeries.map((item) => <Line key={item.code} data={item.points} dataKey="vol" type="monotone" stroke={item.color} strokeWidth={1.5} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />)}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </section>
    <p className="border-t border-border pt-4 text-xs leading-relaxed text-muted-foreground">{t("footnote")}</p>
  </>;
}
