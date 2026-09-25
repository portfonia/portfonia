"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipContentProps } from "recharts";

import { useLocale } from "@/app/_components/locale-provider";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { BENCHMARK_CODES, DEFAULT_BENCHMARKS, getPortfolioRisk, type BenchmarkCode, type PortfolioRiskResponse } from "@/lib/api";
import { MultiSelectMenu } from "../performance/_components/multi-select-menu";
import { BENCHMARK_COLORS, PORTFOLIO_COLOR } from "../performance/_components/performance-colors";
import { formatFullDate, formatShortDate, formatTickPct } from "../performance/_components/performance-format";

// Display copy only; mirrors MIN_SAMPLES and MANUAL_SHARE_LIMIT in portfolio_risk.py.
const MIN_SAMPLE_COPY = 10;
const MANUAL_SHARE_LIMIT_COPY = "66%";

type RiskPoint = { t: number; vol: number };

export function toTimestamp(iso: string): number {
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(year, month - 1, day).getTime();
}

function isoFromTimestamp(t: number): string {
  const date = new Date(t);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function latestOnOrBefore(series: RiskPoint[], hovered: number): RiskPoint | undefined {
  return series.findLast((point) => point.t <= hovered);
}

export type ChartSeries = { name: string; color: string; points: RiskPoint[] };

export function RiskChartTooltip({
  active, label, payload, series,
}: {
  active?: boolean;
  label?: string | number;
  payload?: TooltipContentProps["payload"];
  series: ChartSeries[];
}) {
  const { locale } = useLocale();
  const t = useTranslations("portfolio.riskPanel");
  const hovered = label === undefined ? Number(payload?.[0]?.payload?.t) : Number(label);
  if (!active || !Number.isFinite(hovered)) return null;
  const entries = series.map(({ name, color, points }) => ({
    name, color, point: latestOnOrBefore(points, hovered),
  })).filter((entry) => entry.point !== undefined);
  if (entries.length === 0) return null;
  return (
    <div role="tooltip" className="rounded-lg border border-border bg-card/95 px-3 py-2 text-sm shadow-lg backdrop-blur-sm">
      <p className="mb-1 font-medium tabular-nums">{formatFullDate(isoFromTimestamp(hovered), locale)}</p>
      <ul className="flex flex-col gap-1">
        {entries.map(({ name, color, point }) => point && <li key={name} className="flex flex-col gap-0.5">
          <span className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-2 truncate"><span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ backgroundColor: color }} /><span className="truncate">{name}</span></span>
            <span className="shrink-0 tabular-nums">{formatTickPct(point.vol)}</span>
          </span>
          {point.t !== hovered && <span className="pl-4 text-xs text-muted-foreground">{t("tooltipAsOf", { date: formatFullDate(isoFromTimestamp(point.t), locale) })}</span>}
        </li>)}
      </ul>
    </div>
  );
}

function pct(value: string | null): string {
  return value === null ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
}

const GAUGE_COLORS = ["#36a67a", "#8b6bd6", "#d7ad45", "#df8744", "#d65c88"] as const;
const RISK_COLORS = [GAUGE_COLORS[0], GAUGE_COLORS[2], GAUGE_COLORS[4]];
const DEVIATION_COLORS = [GAUGE_COLORS[4], GAUGE_COLORS[2], GAUGE_COLORS[0], GAUGE_COLORS[2], GAUGE_COLORS[4]];

export function betaSegment(value: number): { index: number; angle: number } {
  const clamped = Math.max(-1, Math.min(3, value));
  return { index: [-0.2, 0.6, 1.4, 2.2].filter((boundary) => clamped >= boundary).length, angle: Math.PI * (1 - (clamped + 1) / 4) };
}

function gaugePoint(angle: number, radius: number): [number, number] {
  return [100 + radius * Math.cos(angle), 92 - radius * Math.sin(angle)];
}

function gaugeTriangle(angle: number, radius: number, inward: boolean): string {
  const [x, y] = gaugePoint(angle, radius);
  const radial: [number, number] = [Math.cos(angle), -Math.sin(angle)];
  const tangent: [number, number] = [-radial[1], radial[0]];
  const tip = inward ? -5 : 5;
  const base = inward ? 5 : -5;
  return [
    [x + radial[0] * tip, y + radial[1] * tip],
    [x + radial[0] * base + tangent[0] * 5, y + radial[1] * base + tangent[1] * 5],
    [x + radial[0] * base - tangent[0] * 5, y + radial[1] * base - tangent[1] * 5],
  ].map(([px, py]) => `${px},${py}`).join(" ");
}

function ExplanationCell({
  name,
  value,
  subtitle,
  explanation,
  reason,
  children,
}: {
  name: string;
  value: string;
  subtitle: string;
  explanation: string;
  reason?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const pointerClick = useRef(false);
  const t = useTranslations("portfolio.riskPanel");
  return (
    <button
      type="button"
      aria-label={`${name}: ${value}. ${explanation}${reason ? ` ${reason}` : ""}`}
      aria-expanded={open}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onPointerDown={() => { pointerClick.current = true; }}
      onFocus={() => { if (!pointerClick.current) setOpen(true); }}
      onBlur={() => setOpen(false)}
      onClick={() => { pointerClick.current = false; setOpen((current) => !current); }}
      className="relative h-64 min-w-0 rounded-xl border border-border bg-background/30 p-4 text-left transition-colors hover:border-primary/50 hover:bg-primary/5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
    >
      <div className="flex items-center justify-between text-sm font-semibold">
        <span>{name}</span><span aria-hidden="true" className="rounded-full border border-current px-1.5 text-xs">{t("infoSymbol")}</span>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>
      {children}
      {open && <span role="tooltip" className="absolute inset-x-3 top-11 z-10 rounded-lg border border-border bg-card p-3 text-xs leading-relaxed text-muted-foreground shadow-lg">{explanation}{reason && <span className="mt-2 block">{reason}</span>}</span>}
    </button>
  );
}

function BetaGauge({ value }: { value: number | null }) {
  const t = useTranslations("portfolio.riskPanel");
  // Rounded caps consume about 11°; a 14° path gap leaves roughly 3° visible.
  const gap = 14 * Math.PI / 180;
  return (
    <div className="relative mx-auto mt-4 max-w-56">
      <svg viewBox="0 -6 200 126" className="w-full" aria-hidden="true">
        {GAUGE_COLORS.map((color, index) => {
          const start = Math.PI * (1 - index / 5) - (index ? gap / 2 : 0);
          const end = Math.PI * (1 - (index + 1) / 5) + (index < 4 ? gap / 2 : 0);
          const [x1, y1] = gaugePoint(start, 72);
          const [x2, y2] = gaugePoint(end, 72);
          return <path key={index} d={`M ${x1} ${y1} A 72 72 0 0 1 ${x2} ${y2}`} fill="none" stroke={color} strokeWidth="14" strokeLinecap="round" />;
        })}
        <polygon data-testid="beta-reference-marker" points={gaugeTriangle(betaSegment(1).angle, 58, false)} fill="var(--muted-foreground)" />
        {value !== null && <polygon data-testid="beta-portfolio-marker" points={gaugeTriangle(betaSegment(value).angle, 88, true)} fill="var(--primary)" />}
      </svg>
      <div className={`absolute inset-x-0 bottom-7 text-center font-heading tabular-nums ${value === null ? "text-sm" : "text-3xl"}`}>{value === null ? t("insufficientSample") : value.toFixed(2)}<span className="block text-xs font-normal text-muted-foreground">{t("beta")}</span></div>
      <div className="flex justify-between text-xs text-muted-foreground"><span>{t("betaMinusOne")}</span><span>{t("betaThree")}</span></div>
    </div>
  );
}

function SegmentedBar({ segments, activeIndex, endLabels, middleLabel }: {
  segments: { color: string; label?: string }[];
  activeIndex: number | null;
  endLabels?: [string, string];
  middleLabel?: string;
}) {
  return (
    <div className="mt-5">
      <div className="flex gap-1.5">
        {segments.map((segment, index) => <div key={index} className="relative min-w-0 flex-1 pt-3" data-testid="bar-segment" data-label={segment.label}>
          {activeIndex === index && <span data-testid="bar-marker" aria-hidden="true" className="absolute left-1/2 top-0 h-0 w-0 -translate-x-1/2 border-x-[5px] border-t-[7px] border-x-transparent border-t-foreground" />}
          <div className="h-2.5 rounded-full" style={{ backgroundColor: segment.color }} />
        </div>)}
      </div>
      {endLabels ? <div className="mt-3 grid text-[10px] text-muted-foreground" style={{ gridTemplateColumns: `repeat(${segments.length}, minmax(0, 1fr))` }}><span>{endLabels[0]}</span>{middleLabel && <span className="text-center" style={{ gridColumn: Math.ceil(segments.length / 2) }}>{middleLabel}</span>}<span className="text-right" style={{ gridColumn: segments.length }}>{endLabels[1]}</span></div> :
        <div className="mt-3 flex justify-between gap-1 text-[10px] text-muted-foreground">{segments.map((segment, index) => <span key={index}>{segment.label}</span>)}</div>}
    </div>
  );
}

export function RiskPanel({ baseCurrency }: { baseCurrency: string }) {
  const t = useTranslations("portfolio.riskPanel");
  const names = useTranslations("portfolio.performance.benchmarkNames");
  const { locale } = useLocale();
  const [benchmarks, setBenchmarks] = useState<BenchmarkCode[]>([...DEFAULT_BENCHMARKS]);
  const [data, setData] = useState<PortfolioRiskResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const requestSeq = useRef(0);
  const requestKey = `${baseCurrency}:${benchmarks.join(",")}`;
  const [resolvedKey, setResolvedKey] = useState<string | null>(null);

  useEffect(() => {
    const seq = ++requestSeq.current;
    getPortfolioRisk(benchmarks, baseCurrency)
      .then((value) => { if (seq === requestSeq.current) { setData(value); setError(false); setResolvedKey(requestKey); } })
      .catch(() => { if (seq === requestSeq.current) { setData(null); setError(true); setResolvedKey(requestKey); } })
      .finally(() => { if (seq === requestSeq.current) setLoading(false); });
  }, [benchmarks, baseCurrency, requestKey]);

  const betaValue = data?.beta.value === null || !data ? null : Number(data.beta.value);
  const betaText = betaValue === null ? t("insufficientSample") : betaValue.toFixed(2);
  const riskText = !data ? "" : data.risk.status === "ok" && data.risk.label ? t(`riskLabels.${data.risk.label}`) : t(`states.${data.risk.status}`);
  const delta = data?.deviation.delta;
  const deviationText = !data ? "" : data.deviation.status !== "ok" || delta === null || delta === undefined
    ? t(`states.${data.deviation.status}`)
    : delta > 0 ? t("aggressiveDirection", { count: delta })
      : delta < 0 ? t("conservativeDirection", { count: Math.abs(delta) }) : t("match");
  const insufficientReason = (count: number) => t("reason.insufficient", { min: MIN_SAMPLE_COPY, count });
  const betaReason = data?.beta.status === "insufficient_sample" ? insufficientReason(data.beta.sample_count) : undefined;
  const riskReason = !data || data.risk.status === "ok" ? undefined
    : data.risk.status === "insufficient_sample" ? insufficientReason(data.portfolio_vol.sample_count)
      : data.risk.status === "data_quality" ? t("reason.manualShare", { share: pct(data.manual_valuation_share), limit: MANUAL_SHARE_LIMIT_COPY })
        : t("reason.noQuestionnaire");
  const deviationReason = data?.deviation.status === "no_questionnaire" ? t("reason.noQuestionnaire")
    : data?.deviation.status === "no_valued_holdings" ? t("reason.noValuedHoldings") : undefined;
  const portfolioSeries = data?.portfolio_vol.points.map((point) => ({ t: toTimestamp(point.date), vol: Number(point.vol) })) ?? [];
  const benchmarkSeries = data?.benchmark_vols.map((item) => ({
    code: item.code,
    name: names(item.code),
    color: BENCHMARK_COLORS[item.code],
    status: item.status,
    current: item.current,
    points: item.points.map((point) => ({ t: toTimestamp(point.date), vol: Number(point.vol) })),
    window_start: item.window_start,
    window_end: item.window_end,
    sample_count: item.sample_count,
  })) ?? [];
  const chartSeries: ChartSeries[] = [
    ...(data?.portfolio_vol.status === "ok" ? [{ name: t("portfolio"), color: PORTFOLIO_COLOR, points: portfolioSeries }] : []),
    ...benchmarkSeries.filter((item) => item.status === "ok").map(({ name, color, points }) => ({ name, color, points })),
  ];
  const timestamps = [...new Set(chartSeries.flatMap((item) => item.points.map((point) => point.t)))].sort((a, b) => a - b);
  const ticks = timestamps.length <= 6 ? timestamps : Array.from({ length: 6 }, (_, index) => timestamps[Math.round(index * (timestamps.length - 1) / 5)]);
  const chartName = benchmarks.map((code) => names(code)).join(", ");
  const pending = loading || resolvedKey !== requestKey;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
        <CardDescription>{t("subtitle")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6 px-4">
        {pending ? <div role="status" aria-label={t("loading")} className="grid grid-cols-1 gap-4 min-[461px]:grid-cols-2 min-[721px]:grid-cols-3">
          {[0, 1, 2].map((n) => <div key={n} className="h-64 animate-pulse rounded-xl bg-muted" />)}
        </div> : error ? <p role="alert" className="text-sm text-destructive">{t("loadError")}</p> : data && <>
          <div className="grid grid-cols-1 gap-4 min-[461px]:grid-cols-2 min-[721px]:grid-cols-3">
            <ExplanationCell name={t("beta")} value={betaText} subtitle={t("betaSubtitle")} explanation={t("betaExplanation")} reason={betaReason}>
              <BetaGauge value={betaValue} />
            </ExplanationCell>
            <ExplanationCell name={t("risk")} value={riskText} subtitle={t("riskSubtitle")} explanation={t("riskExplanation")} reason={riskReason}>
              <p className="mt-7 min-h-12 font-heading text-xl text-primary">{riskText}</p>
              <SegmentedBar segments={RISK_COLORS.map((color, index) => ({ color, label: t(`riskLabels.${(["within", "caution", "exceeds"] as const)[index]}`) }))} activeIndex={data.risk.status === "ok" && data.risk.label ? ({ within: 0, caution: 1, exceeds: 2 })[data.risk.label] : null} />
              <p className="mt-4 text-xs text-muted-foreground">{t("objectiveTier", { tier: data.portfolio_vol.tier ? t(`tiers.${data.portfolio_vol.tier}`) : t("insufficientSample") })}</p>
            </ExplanationCell>
            <ExplanationCell name={t("deviation")} value={deviationText} subtitle={t("deviationSubtitle")} explanation={t("deviationExplanation")} reason={deviationReason}>
              <p className="mt-7 min-h-12 font-heading text-xl text-primary">{deviationText}</p>
              <SegmentedBar segments={DEVIATION_COLORS.map((color) => ({ color }))} activeIndex={data.deviation.status === "ok" && delta !== null && delta !== undefined ? delta + 2 : null} endLabels={[t("conservative"), t("aggressive")]} middleLabel={t("matchShort")} />
              <p className="mt-4 text-xs text-muted-foreground">{t("deviationSubline")}</p>
            </ExplanationCell>
          </div>
        </>}
        <section className="min-w-0 border-t border-border pt-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 className="font-heading text-lg">{t("volatilityTitle")}</h3>
            <MultiSelectMenu label={t("benchmarkSelector")} options={BENCHMARK_CODES.map((code) => ({ value: code, label: names(code) }))} selected={benchmarks} onChange={(next) => { setLoading(true); setData(null); setBenchmarks(next as BenchmarkCode[]); }} allMode="all-options" disabled={pending} />
          </div>
          {!pending && !error && data && <>
            <div className="mt-4 flex flex-wrap gap-5 text-sm">
              <span><span aria-hidden="true" className="mr-2 inline-block size-2 rounded-full" style={{ backgroundColor: PORTFOLIO_COLOR }} />{t("portfolio")}: <strong>{data.portfolio_vol.status === "ok" ? pct(data.portfolio_vol.current) : t("insufficientSample")}</strong></span>
              {benchmarkSeries.map((item) => <span key={item.code}><span aria-hidden="true" className="mr-2 inline-block size-2 rounded-full" style={{ backgroundColor: item.color }} />{item.name}: <strong>{item.status === "ok" ? pct(item.current) : t("insufficientSample")}</strong></span>)}
            </div>
            {chartSeries.length ? <div role="img" aria-label={benchmarks.length ? t("chartDescription", { benchmark: chartName }) : t("chartDescriptionPortfolioOnly")} className="mt-5 h-56 w-full min-w-0">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} />
                  <XAxis type="number" dataKey="t" scale="time" domain={["dataMin", "dataMax"]} ticks={ticks} minTickGap={24} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} tickLine={false} axisLine={false} tickFormatter={(value: number) => formatShortDate(isoFromTimestamp(value), locale)} />
                  <YAxis tickFormatter={formatTickPct} domain={[0, "auto"]} width={48} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} tickLine={false} axisLine={false} />
                  <Tooltip content={(props: TooltipContentProps) => <RiskChartTooltip {...props} series={chartSeries} />} />
                  {data.portfolio_vol.status === "ok" && <Line data={portfolioSeries} dataKey="vol" type="monotone" stroke={PORTFOLIO_COLOR} strokeWidth={2.5} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />}
                  {benchmarkSeries.filter((item) => item.status === "ok").map((item) => <Line key={item.code} data={item.points} dataKey="vol" type="monotone" stroke={item.color} strokeWidth={1.5} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />)}
                </LineChart>
              </ResponsiveContainer>
            </div> : <p className="mt-5 text-sm text-muted-foreground">{t("insufficientSample")}</p>}
            <p className="mt-3 text-xs text-muted-foreground">{t("sampleDisclosurePortfolio", { start: data.portfolio_vol.window_start ?? "—", end: data.portfolio_vol.window_end ?? "—", count: data.portfolio_vol.sample_count, currency: data.base_currency })}{benchmarkSeries.map((item) => ` · ${t("sampleDisclosureBenchmark", { benchmark: item.name, start: item.window_start ?? "—", end: item.window_end ?? "—", count: item.sample_count })}`).join("")}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t("manualShare", { share: pct(data.manual_valuation_share) })}</p>
          </>}
        </section>
        {!pending && !error && data && <p className="border-t border-border pt-4 text-xs leading-relaxed text-muted-foreground">{t("footnote")}</p>}
      </CardContent>
    </Card>
  );
}
